# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Load mode: build each image into the local daemon, in order.

The docker-workflows verify and merge lanes' loop. Each image builds as
``[<namespace>/]<name>:<local_tag>``; with a namespace it is aliased as
``<name>:<local_tag>`` too, so same-repository chains written either
way resolve. Order is the caller's: a base must come before the images
built from it, which docker-build-matrix-action's order: dependencies
guarantees.

Every image gets an attempt by default, because a later image can
build when an earlier one does not, and a full picture of what breaks
beats stopping at the first failure. An image whose base failed fails
in turn, which is the accurate signal. skip_dependents instead skips
it, when the entries carry depends_on from the matrix output.
"""

from __future__ import annotations

from scripts import docker, gha
from scripts.flags import image_flags
from scripts.outcome import Outcome
from scripts.settings import Image, Settings


def dockerfile_of(image: Image) -> str:
    """The Dockerfile path, relative to path_prefix, as the lanes' jq //.

    An empty string passes through as '-f ""', which buildx reads as
    <context>/Dockerfile, the same file the default names.
    """
    dockerfile = image.get("dockerfile")
    if dockerfile is None or dockerfile is False:
        return f"{image['context']}/Dockerfile"
    return str(dockerfile)


def build_args(image: Image, shared: tuple[str, ...] = ()) -> list[str]:
    """``--target``, ``--build-arg`` and extra flags for an entry.

    Extra flags follow the dedicated ones: the build_flags input's,
    then the entry's own.
    """
    flags: list[str] = []
    target = image.get("target")
    if isinstance(target, str) and target:
        flags += ["--target", target]
    for arg in image.get("build_args") or []:
        if isinstance(arg, str) and arg:
            flags += ["--build-arg", arg]
    return flags + list(shared) + image_flags(image)


def build_all(settings: Settings, cwd: str, outcome: Outcome) -> None:
    """Build every entry into the daemon, recording each result."""
    namespace = settings.image_namespace
    unbuilt: set[str] = set()
    for image in settings.images:
        name = image["name"]
        plain = f"{name}:{settings.local_tag}"
        tag = f"{namespace}/{plain}" if namespace else plain
        if settings.skip_dependents:
            blocked = [
                base for base in image.get("depends_on") or [] if base in unbuilt
            ]
            if blocked:
                gha.annotate(
                    "notice",
                    f"Skipping {tag}: it builds from {', '.join(blocked)}, which did not build",
                )
                outcome.record(name, tag, "skipped")
                unbuilt.add(name)
                continue
        args = ["buildx", "build", "--load", "-f", dockerfile_of(image), "-t", tag]
        args += build_args(image, settings.build_flags)
        status = docker.run([*args, image["context"]], cwd, group=f"Build {tag}")
        if status != 0:
            gha.annotate("error", f"Build failed for {tag} (exit {status})")
            outcome.record(name, tag, "failed")
            unbuilt.add(name)
            continue
        # A failed alias fails the image like a failed build: later
        # images still get their attempt and the outputs still publish.
        if namespace and docker.run(["tag", tag, plain], cwd) != 0:
            gha.annotate("error", f"Could not alias {tag} as {plain}")
            outcome.record(name, tag, "failed")
            unbuilt.add(name)
            continue
        outcome.record(name, tag, "built")
