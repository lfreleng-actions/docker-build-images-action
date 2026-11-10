# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The build_command escape hatch: project tooling builds the images.

The docker-workflows verify and merge lanes' implementation. Tooling
such as Maven's fabric8 plugin, jib or a Makefile produces the images;
this works out which ones, either from build_command_images, which the
caller declares, or by inference from the daemon's image events during
the command.

A new tag counts as built here when the ID of the image it names
appears in a create or load event: builds, commits and docker load emit
those, while a pull or a re-tag of an existing image does not, on
either image store. Matching on the ID rather than the order of events
keeps a second tag of a built image attributed. Beside a build, a new
tag whose image was new to the daemon yet neither created nor loaded is
a pulled base and is skipped. The one residual limit is a command that
resolves to an image ID already present before it ran, a pure cache
hit: nothing is created, and a re-tag looks the same, so that image is
reported by name for the caller to declare, and the step fails even
when the command built others. Creation timestamps are never
consulted: jib pins them to the Unix epoch.

When the events cannot be watched, inference falls back to the
registry digest, which holds only on the classic image store, where a
local build carries none. An image without one can only have been
built here, and proves the store gives local builds none, which makes
the digest-bearing images pulled bases. Without that proof the two
readings cannot be told apart, so those images are reported by name
rather than guessed at (docker-workflows PR #65).
"""

from __future__ import annotations

import subprocess
import sys

from scripts import docker, events, gha
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


def _pulled(image: str) -> None:
    gha.annotate(
        "notice",
        f"Skipping pulled image {image} (registry digest, on a store that gives "
        "local builds none)",
    )


def _by_digest(candidates: list[str], cwd: str) -> tuple[list[str], list[str]]:
    built: list[str] = []
    held: list[str] = []
    for candidate in candidates:
        (built if docker.digest_count(candidate, cwd) == "0" else held).append(
            candidate
        )
    if built:
        # Something built here carries no digest, so this store gives
        # local builds none and the held images are pulled bases.
        for image in held:
            _pulled(image)
        held = []
    return built, held


def _by_event(
    candidates: list[str], produced: set[str], existing: set[str], cwd: str
) -> tuple[list[str], list[str]]:
    ids = {candidate: docker.image_id(candidate, cwd) for candidate in candidates}
    built = [candidate for candidate in candidates if ids[candidate] in produced]
    held = [candidate for candidate in candidates if ids[candidate] not in produced]
    if not built:
        return built, held
    # Only a pulled base is skipped beside a build. Its image is new to
    # the daemon; a re-tag or a cache hit names one that was already
    # there, which the command may have built, so it stays unattributed.
    proven = any(docker.digest_count(image, cwd) == "0" for image in built)
    unresolved: list[str] = []
    for image in held:
        if ids[image] in existing:
            unresolved.append(image)
        elif proven and docker.digest_count(image, cwd) != "0":
            # The digest proof holds too: named as the classic store
            # always named them.
            _pulled(image)
        else:
            gha.annotate(
                "notice",
                f"Skipping {image}: build_command neither created nor loaded its image",
            )
    return built, unresolved


def _infer(
    before: list[str], existing: set[str], produced: set[str] | None, cwd: str
) -> tuple[list[str], list[str]]:
    """Split new images into (built here, unattributable).

    ``existing`` holds the image IDs present before the command, and
    ``produced`` the IDs created or loaded during it, or None when the
    events were lost and only the digest rule is left.
    """
    candidates = [
        tag
        for tag in sorted(set(docker.local_tags(cwd)) - set(before))
        if "<none>" not in tag
    ]
    if produced is None:
        return _by_digest(candidates, cwd)
    return _by_event(candidates, produced, existing, cwd)


def _unattributed(images: list[str], alongside: bool, cwd: str) -> str:
    if not alongside and all(
        docker.digest_count(image, cwd) != "0" for image in images
    ):
        # True under either rule, so worded as the lanes word it.
        reason = (
            "Every new image here carries a registry digest, which means a pulled "
            "base on one image store and proves nothing on another, so either the "
            "command built these and they need naming with the build_command_images "
            "input, or it built nothing at all"
        )
    else:
        reason = (
            "The command neither created nor loaded their images: it re-tagged "
            "them, or rebuilt them to an image already present before it ran. Name "
            "any it built with the build_command_images input"
        )
    return (
        f"build_command left {len(images)} image(s) this run cannot attribute: "
        f"{' '.join(images)}. {reason}"
    )


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
    existing = set() if declared else docker.image_ids(cwd)
    watch = None if declared else events.Watch.start(cwd)
    try:
        _run_command(command, cwd, outcome)
        produced = watch.stop() if watch else None
    finally:
        if watch:
            watch.close()
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
        built, unattributed = _infer(before, existing, produced, cwd)
        for ref in built:
            outcome.record(_repository(ref), ref, "built")
    # The attribution message is reported even when the command also
    # failed, or built other images: it names the images and the input
    # that settles them, which is exactly what a caller needs then.
    if unattributed:
        gha.annotate("error", _unattributed(unattributed, bool(outcome.built), cwd))
    elif outcome.built:
        return
    elif not outcome.failures:
        gha.annotate(
            "error",
            "build_command completed but created no new images. Name them with the "
            "build_command_images input if the command builds images this workflow "
            "cannot observe",
        )
    if not outcome.failures:
        outcome.failures.append("build_command")
