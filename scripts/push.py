# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Push mode: build, push to each repository, and capture digests.

The docker-workflows release lane's loop. Every image pushes as
``<repository>/<name>:<tag>`` for each repository and tag, and each
pushed repository's digest is captured from the push itself, never
re-read through the mutable tag, so a concurrent writer moving the tag
cannot desynchronise what a signing job signs from what this run
pushed.

Multi-platform builds push a manifest list straight to the registries,
then pull back the scan platform's image by digest, so later jobs test
and scan the published bits. With no repository there is nowhere to
hold a manifest list, so only the scan platform builds, with a warning.

The local working tag is ``<name>:<local_tag>``, as in load mode, so
same-repository chains resolve alike; the namespaced alias is added
when the namespace is a usable local prefix and skipped (with a
notice) otherwise, since local tags never publish.

push_by_digest pushes every image untagged, so a release can tag it
only once its gates pass. One platform needs the docker-container
builder too: buildx refuses push-by-digest on the docker driver
whatever the image store (build/opt.go, v0.37.2). The other route, a
``docker save`` archive of the loaded image pushed with crane, needs a
tool GitHub's runners lack, recompresses every layer, and tags
``latest`` unless pushed to an explicit digest; on a two-image chain
it took no less time (26s cold, against 25s). So one platform builds
as several do: a named build context carries a chain, and the scan
copy is pulled back by digest.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from scripts import docker, gha
from scripts.gha import ActionError
from scripts.native import build_args, dockerfile_of
from scripts.outcome import Outcome
from scripts.refs import PATH, familiar, repository_path
from scripts.settings import Settings

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_REPOSITORY = 255

_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def native_platform() -> str:
    """The runner's platform: DOCKER_DEFAULT_PLATFORM, else the host's."""
    configured = os.environ.get("DOCKER_DEFAULT_PLATFORM", "").strip()
    if configured:
        return configured
    machine = os.uname().machine.lower()
    return f"linux/{_ARCH.get(machine, machine)}"


def _scan_platform(platforms: tuple[str, ...], native: str) -> str:
    """The platform whose image later jobs test and scan.

    The runner's own when the build targets it; otherwise the first
    requested. Syft and Grype analyse archives statically, so a foreign
    architecture still scans.
    """
    return native if native in platforms else platforms[0]


def _alias_namespace(namespace: str, local_tag: str) -> bool:
    if namespace and PATH.fullmatch(namespace):
        return True
    if namespace:
        gha.annotate(
            "notice",
            f"image_namespace '{namespace}' is not a usable local reference prefix; "
            f"skipping the namespaced :{local_tag} aliases",
        )
    return False


def _pushed_digest(repo: str, local_tag: str, cwd: str) -> str:
    """The digest the daemon recorded for our own push of ``repo``.

    Compared in docker's familiar form, the form it reports RepoDigests
    in, so docker.io/onap/x matches the onap/x it prints.
    """
    wanted = familiar(repo)
    for entry in docker.repo_digests(local_tag, cwd):
        name, _, digest = entry.partition("@")
        if familiar(name) == wanted:
            return digest
    return ""


def _build(cmd: list[str], cwd: str, group: str, local_tag: str) -> None:
    status = docker.run(cmd, cwd, group=group)
    if status != 0:
        # Stops the run: no later image builds or pushes. Earlier pushes
        # stay in their registries, which offer nothing to roll back.
        raise ActionError(f"Build failed for {local_tag} (exit {status})")


def build_all(settings: Settings, cwd: str, outcome: Outcome) -> None:
    """Build and push every entry, recording each pushed digest.

    Stops at the first failure: the failed image records as failed and
    the rest as skipped. Registries offer no transaction to roll back,
    so earlier pushes remain. ``pushed`` lists every repository whose
    push completed, recorded as soon as its digest is known, so a later
    failure in the same image (a pull-back, a tag) still reports it; one
    that failed part-way can leave some of its tags behind, which the
    failed entry flags.
    """
    native = native_platform()
    platforms = settings.platforms or (native,)
    plan = _Plan(
        platforms=platforms,
        multi=platforms != (native,),
        scan=_scan_platform(platforms, native),
        alias=_alias_namespace(settings.image_namespace, settings.local_tag),
        metadata=os.path.join(tempfile.mkdtemp(), "build-meta.json"),
    )
    for position, image in enumerate(settings.images):
        local_tag = f"{image['name']}:{settings.local_tag}"
        try:
            _build_one(settings, plan, image, local_tag, cwd, outcome)
        except ActionError as err:
            gha.annotate("error", str(err))
            outcome.record(image["name"], local_tag, "failed")
            for rest in settings.images[position + 1 :]:
                outcome.record(
                    rest["name"], f"{rest['name']}:{settings.local_tag}", "skipped"
                )
            return


@dataclass
class _Plan:
    platforms: tuple[str, ...]
    multi: bool
    scan: str
    alias: bool
    metadata: str
    # Named build contexts for images already pushed from the
    # docker-container builder, which cannot see the daemon's tags, so a
    # later FROM <name>:verify would otherwise pull from Docker Hub;
    # this points it at the pushed image by digest, per platform.
    contexts: list[str] = field(default_factory=list)


def _build_one(
    settings: Settings, plan: _Plan, image: dict[str, Any], local_tag: str, cwd: str,
    outcome: Outcome,
) -> None:  # fmt: skip
    name = image["name"]
    common = ["-f", dockerfile_of(image), *build_args(image, settings.build_flags)]
    repos = [f"{prefix}/{name}" for prefix in settings.repositories]
    refs = [f"{repo}:{tag}" for repo in repos for tag in settings.tags]
    tag_args = [arg for ref in refs for arg in ("-t", ref)]
    if (plan.multi or settings.push_by_digest) and repos:
        if settings.push_by_digest:
            destination = ["--output", _by_digest(repos)]
        else:
            destination = ["--push"]
        _build(
            [
                "buildx", "build", "--platform", ",".join(plan.platforms), *destination,
                "--provenance=false", "--sbom=false", "--metadata-file", plan.metadata,
                *plan.contexts, *tag_args, *common, image["context"],
            ],
            cwd, f"Build/push {name} ({','.join(plan.platforms)})", local_tag,
        )  # fmt: skip
        image_digest = _metadata_digest(plan.metadata)
        # The registries hold the image now, so record it before the
        # fallible pull-back: a failure there must not hide it.
        _record_pushes(
            settings,
            name,
            repos,
            lambda repo: _registry_digest(settings, repo, image_digest, cwd),
            outcome,
        )
        source = f"{repos[0]}@{image_digest}"
        if docker.run(
            ["pull", "--platform", plan.scan, source], cwd, f"Pull {name} ({plan.scan})"
        ):
            raise ActionError(f"Could not pull {source} back for scanning")
        if docker.run(["tag", source, local_tag], cwd) != 0:
            raise ActionError(f"Could not tag {source} as {local_tag}")
        names = [local_tag]
        if plan.alias:
            names.append(f"{settings.image_namespace}/{local_tag}")
        plan.contexts += [f"--build-context={n}=docker-image://{source}" for n in names]
    elif plan.multi:
        if ",".join(plan.platforms) != plan.scan:
            gha.annotate(
                "warning",
                f"No registry resolved, so {name} builds only {plan.scan}; the other "
                f"platforms in '{','.join(plan.platforms)}' are not built and no manifest "
                "list is produced",
            )
        _build(
            ["buildx", "build", "--platform", plan.scan, "--load", "-t", local_tag, *common,
             image["context"]],
            cwd, f"Build {local_tag} ({plan.scan})", local_tag,
        )  # fmt: skip
    else:
        _build(
            ["buildx", "build", "--load", "-t", local_tag, *tag_args, *common, image["context"]],
            cwd, f"Build {local_tag}", local_tag,
        )  # fmt: skip
        for ref in refs:
            if docker.run(["push", ref], cwd, group=f"Push {ref}") != 0:
                raise ActionError(f"Push failed for {ref}")
        _record_pushes(
            settings,
            name,
            repos,
            lambda repo: _pushed_digest(repo, local_tag, cwd),
            outcome,
        )
    # Alias before recording success, so a failed alias leaves exactly
    # one terminal status: failed, from the caller's handler.
    if plan.alias:
        _alias(settings.image_namespace, name, local_tag, settings.local_tag, cwd)
    outcome.record(name, local_tag, "built")


def _record_pushes(
    settings: Settings, name: str, repos: list[str], digest_of: Callable[[str], str],
    outcome: Outcome,
) -> None:  # fmt: skip
    """Add each pushed repository to ``pushed``, with its push's digest."""
    for repo in repos:
        digest = digest_of(repo)
        if not _DIGEST.fullmatch(digest):
            pushed = f"{repo}:{settings.tags[0]}" if settings.tags else repo
            raise ActionError(f"Failed to resolve digest for {pushed} (got: {digest})")
        gha.log(f"{repo}@{digest}")
        outcome.pushed.append({"name": name, "image": repo, "digest": digest})


def _by_digest(repos: list[str]) -> str:
    """The --output that pushes one untagged manifest to every repository.

    buildx reads --output as one CSV record, so the comma-separated
    names are quoted into a single field; unquoted, it refuses the
    second name as an invalid value. Repository names hold no quote or
    comma, which settings validation guarantees.
    """
    names = ",".join(repos)
    return (
        f'type=image,"name={names}",push-by-digest=true,name-canonical=true,push=true'
    )


def _registry_digest(settings: Settings, repo: str, digest: str, cwd: str) -> str:
    """The digest to record for ``repo`` after a push from BuildKit.

    buildx pushes the same bytes to every name, so its one digest covers
    them all by construction. With push_by_digest the digest is read
    back from each repository's own registry, which makes ``pushed`` a
    per-registry record rather than that assumption, and catches a
    registry that did not keep an untagged manifest.
    """
    if not settings.push_by_digest or not _DIGEST.fullmatch(digest):
        return digest
    served = docker.served_digest(f"{repo}@{digest}", cwd)
    if served != digest:
        raise ActionError(f"{repo}@{digest} is served as {served} by its registry")
    return served


def _metadata_digest(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return str(json.load(handle).get("containerimage.digest", ""))
    except (OSError, ValueError):
        return ""


def _alias(namespace: str, name: str, local_tag: str, suffix: str, cwd: str) -> None:
    alias_repo = f"{namespace}/{name}"
    # Docker caps the path, not the name: a registry-like first
    # component of the namespace does not count.
    path = repository_path(alias_repo)
    if len(path) > MAX_REPOSITORY:
        gha.annotate(
            "notice",
            f"Skipping the namespaced alias for {name}: {alias_repo} resolves to a "
            f"{len(path)}-character repository path, over Docker's limit of 255",
        )
        return
    if docker.run(["tag", local_tag, f"{alias_repo}:{suffix}"], cwd) != 0:
        raise ActionError(f"Could not alias {local_tag} as {alias_repo}:{suffix}")
