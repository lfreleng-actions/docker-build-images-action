# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The build_command escape hatch: project tooling builds the images.

The docker-workflows verify and merge lanes' implementation. Tooling
such as Maven's fabric8 plugin, jib or a Makefile produces the images;
this works out which ones, either from build_command_images, which the
caller declares, or by inference from the image list around the
command.

Inference has one piece of evidence, the registry digest, and it holds
only on the classic image store, where a local build carries none. The
containerd store gives every image a digest. An image without one can
only have been built here, and proves the store gives local builds
none, which makes the digest-bearing images pulled bases. Without that
proof the two readings cannot be told apart, so those images are
reported by name rather than guessed at (docker-workflows PR #65).
Creation timestamps are never consulted: jib pins them to the Unix
epoch.
"""

from __future__ import annotations

import subprocess
import sys

from scripts import docker, gha
from scripts.gha import ActionError
from scripts.outcome import Outcome
from scripts.refs import declared_images


def _clear(declared: list[str], cwd: str) -> None:
    """Drop existing tags, so a reference present afterwards is new.

    On a reused daemon, a command that quietly built nothing would
    otherwise hand its previous image on as though it were fresh.
    """
    for ref in declared:
        if not docker.exists(ref, cwd):
            continue
        gha.annotate(
            "notice", f"Clearing existing {ref} so the command has to produce it"
        )
        if docker.query(["image", "rm", ref], cwd)[0] != 0:
            raise ActionError(
                f"Could not clear the existing {ref}; with that tag still in place a "
                "command that builds nothing would look successful. Remove any "
                "container holding it"
            )


def _run_command(command: str, cwd: str, outcome: Outcome) -> None:
    gha.group("build_command")
    sys.stdout.flush()
    status = subprocess.run(
        ["bash", "-e", "-c", command], cwd=cwd, check=False
    ).returncode
    sys.stdout.flush()
    gha.endgroup()
    if status != 0:
        gha.annotate("error", f"build_command exited {status}")
        outcome.failures.append("build_command")


def _infer(before: list[str], cwd: str) -> tuple[list[str], list[str]]:
    """Split new images into (built here, unattributable)."""
    candidates = sorted(set(docker.local_tags(cwd)) - set(before))
    built: list[str] = []
    held: list[str] = []
    for candidate in candidates:
        if "<none>" in candidate:
            continue
        (built if docker.digest_count(candidate, cwd) == "0" else held).append(
            candidate
        )
    if built:
        # Something built here carries no digest, so this store gives
        # local builds none and the held images are pulled bases.
        for image in held:
            gha.annotate(
                "notice",
                f"Skipping pulled image {image} (registry digest, on a store that "
                "gives local builds none)",
            )
        held = []
    return built, held


def _repository(ref: str) -> str:
    # Every reference here ends in :<tag> (declared ones are validated to,
    # and image ls prints repository:tag), so the last colon is the tag's.
    return ref.rpartition(":")[0]


def run(command: str, declared_raw: str, cwd: str, outcome: Outcome) -> None:
    """Run ``command`` and record the images it produced.

    Results carry the repository as the name and the full reference as
    the tag, as load and push mode record theirs.
    """
    declared = declared_images(declared_raw)
    _clear(declared, cwd)
    before = docker.local_tags(cwd)
    _run_command(command, cwd, outcome)
    unattributed: list[str] = []
    if declared:
        for ref in declared:
            if docker.exists(ref, cwd):
                outcome.record(_repository(ref), ref, "built")
            else:
                gha.annotate(
                    "error",
                    f"build_command_images names {ref}, which the command did not create",
                )
                outcome.record(_repository(ref), ref, "failed")
    else:
        built, unattributed = _infer(before, cwd)
        for ref in built:
            outcome.record(_repository(ref), ref, "built")
    if outcome.built:
        return
    # The attribution message is reported even when the command also
    # failed: it names the images and the input that settles them,
    # which is exactly what a caller needs then.
    if unattributed:
        gha.annotate(
            "error",
            f"build_command left {len(unattributed)} image(s) this run cannot attribute: "
            f"{' '.join(unattributed)}. Every new image here carries a registry digest, "
            "which means a pulled base on one image store and proves nothing on another, "
            "so either the command built these and they need naming with the "
            "build_command_images input, or it built nothing at all",
        )
    elif not outcome.failures:
        gha.annotate(
            "error",
            "build_command completed but created no new images. Name them with the "
            "build_command_images input if the command builds images this workflow "
            "cannot observe",
        )
    if not outcome.failures:
        outcome.failures.append("build_command")
