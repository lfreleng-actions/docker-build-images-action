# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Action inputs: parsing and the rules that tie them together."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from scripts import gha
from scripts.flags import image_flags, shared_flags
from scripts.gha import ActionError
from scripts.refs import (
    COMPONENT,
    MAX_PATH,
    PATH,
    REGISTRY,
    is_registry,
    repository_path,
    split_list,
)

MODES = ("load", "push")
# Docker's grammar for a tag.
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def valid_repository(prefix: str) -> bool:
    """A repository prefix: an optional registry, then path components.

    The same grammar build_command_images entries meet (scripts/refs.py):
    a registry is dot-separated labels, each starting and ending
    alphanumeric, with an optional port; so bad-.example, bad..example
    and example. are refused before anything builds or pushes. A
    registry alone is a prefix too, since the image name follows it.
    """
    parts = prefix.split("/")
    if is_registry(parts[0]):
        if not REGISTRY.fullmatch(parts[0]):
            return False
        parts = parts[1:]
    return all(COMPONENT.fullmatch(part) for part in parts)


IMAGES_ERROR = (
    "images must be a JSON array of image entries, each with string 'name' and "
    "'context', as docker-build-matrix-action's images_json output, or its "
    "matrix output"
)

Image = dict[str, Any]


def _absent(value: object) -> bool:
    # jq's // falls through on null and false only; the lanes read these
    # fields that way, so the same values count as absent here.
    return value is None or value is False


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _entry_problem(entry: object) -> str | None:
    """Why an images entry cannot build, or None when it can.

    Checked for every entry before anything builds: an unchecked value
    would otherwise fail part-way through, after earlier images pushed.
    """
    if not isinstance(entry, dict):
        return "is not an object"
    for key in ("name", "context"):
        if not isinstance(entry.get(key), str):
            return f"needs a string '{key}'"
    if not PATH.fullmatch(entry["name"]):
        return (
            "has a 'name' that is not a valid Docker repository name (lowercase "
            "alphanumeric runs joined by '.', '_', '__' or '-', in '/'-separated parts)"
        )
    # The context is buildx's final, bare argument: '--help' there exits
    # 0 having built nothing, and '-' reads a Dockerfile from stdin.
    if entry["context"].startswith("-"):
        return "has a 'context' starting with '-', which buildx would read as a flag"
    for key in ("dockerfile", "target"):
        value = entry.get(key)
        if not (_absent(value) or isinstance(value, str)):
            return f"has a '{key}' that is not a string"
    for key in ("build_args", "depends_on"):
        value = entry.get(key)
        if not (_absent(value) or _string_list(value)):
            return f"has a '{key}' that is not a list of strings"
    return None


def parse_images(raw: str) -> list[Image]:
    """The image list: images_json's array, or the matrix's include."""
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise ActionError(IMAGES_ERROR) from err
    if isinstance(data, dict) and isinstance(data.get("include"), list):
        data = data["include"]
    if not isinstance(data, list):
        raise ActionError(IMAGES_ERROR)
    for index, entry in enumerate(data):
        problem = _entry_problem(entry)
        if problem:
            name = entry.get("name") if isinstance(entry, dict) else None
            label = name if isinstance(name, str) and name else f"#{index + 1}"
            raise ActionError(f"{IMAGES_ERROR}; entry {label} {problem}")
    # Refuse a malformed or owned per-image flag before anything builds.
    for entry in data:
        image_flags(entry)
    return data


@dataclass(frozen=True)
class Settings:
    """The action inputs, parsed and validated."""

    path_prefix: str = "."
    images: list[Image] = field(default_factory=list)
    image_namespace: str = ""
    mode: str = "load"
    local_tag: str = "verify"
    build_command: str = ""
    build_command_images: str = ""
    permit_fail: bool = False
    skip_dependents: bool = False
    repositories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    summary: bool = True
    setup_buildx: bool = True
    build_flags: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        """Read and cross-check the INPUT_* variables."""
        env = gha.env
        mode = env("INPUT_MODE").strip() or "load"
        if mode not in MODES:
            raise ActionError(f"mode must be one of {', '.join(MODES)}; got '{mode}'")
        settings = cls(
            path_prefix=env("INPUT_PATH_PREFIX", ".") or ".",
            images=parse_images(env("INPUT_IMAGES")),
            image_namespace=env("INPUT_IMAGE_NAMESPACE"),
            mode=mode,
            local_tag=env("INPUT_LOCAL_TAG").strip() or "verify",
            build_command=env("INPUT_BUILD_COMMAND"),
            build_command_images=env("INPUT_BUILD_COMMAND_IMAGES"),
            permit_fail=gha.env_bool(
                "INPUT_BUILD_PERMIT_FAIL", "build_permit_fail", False
            ),
            skip_dependents=gha.env_bool(
                "INPUT_SKIP_DEPENDENTS", "skip_dependents", False
            ),
            repositories=tuple(split_list(env("INPUT_REPOSITORIES"))),
            tags=tuple(split_list(env("INPUT_TAGS"))),
            platforms=tuple(
                p.strip() for p in env("INPUT_PLATFORMS").split(",") if p.strip()
            ),
            summary=gha.env_bool("INPUT_SUMMARY", "summary", True),
            setup_buildx=gha.env_bool("INPUT_SETUP_BUILDX", "setup_buildx", True),
            build_flags=tuple(shared_flags(env("INPUT_BUILD_FLAGS"))),
        )
        settings.check()
        return settings

    def check(self) -> None:
        """Refuse combinations one mode or the other cannot honour."""
        if not _TAG.fullmatch(self.local_tag):
            raise ActionError(f"local_tag '{self.local_tag}' is not a valid Docker tag")
        if self.mode == "push":
            # A partial publication is never treated as success: push
            # mode stops at the first failure and fails the step, so
            # permitting failures would defeat it. Project tooling cannot
            # produce manifest lists or per-registry digests reliably
            # (docker-workflows build-test-release).
            for name, flag in (
                ("build_permit_fail", self.permit_fail),
                ("build_command", self.build_command),
                ("build_command_images", self.build_command_images),
                ("skip_dependents", self.skip_dependents),
            ):
                if flag:
                    raise ActionError(f"{name} is not available with mode: push")
            if self.repositories and not self.tags:
                raise ActionError("mode: push with repositories needs at least one tag")
        else:
            for name, value in (
                ("repositories", self.repositories),
                ("tags", self.tags),
                ("platforms", self.platforms),
            ):
                if value:
                    raise ActionError(f"{name} applies to mode: push only")
        for tag in self.tags:
            if not _TAG.fullmatch(tag):
                raise ActionError(f"tags entry '{tag}' is not a valid Docker tag")
        for repository in self.repositories:
            if not valid_repository(repository):
                raise ActionError(
                    f"repositories entry '{repository}' is not a valid repository prefix "
                    "(such as ghcr.io/org or docker.io/onap)"
                )
        self._check_lengths()

    def _check_lengths(self) -> None:
        # Before anything builds: in push mode an overlong later image
        # would otherwise fail after earlier ones had published. The bare
        # name is the local tag (or its alias) in both modes.
        if self.mode == "push":
            prefixes = ("", *self.repositories)
        else:
            prefixes = ("", self.image_namespace)
        for image in self.images:
            for prefix in prefixes:
                full = f"{prefix}/{image['name']}" if prefix else image["name"]
                path = repository_path(full)
                if len(path) > MAX_PATH:
                    raise ActionError(
                        f"images entry {image['name']} resolves to {full}, a "
                        f"{len(path)}-character repository path, over Docker's "
                        f"limit of {MAX_PATH}"
                    )
