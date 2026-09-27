# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Extra docker buildx flags: the build_flags input and per-image lists.

global-jjb passes a job's docker-build-args to docker build verbatim;
OpenDaylight relies on it for --network=host. build_flags carries the
same need, for every image, and an images entry's build_flags list
adds flags for that image alone, after the shared ones.

Flags the action itself decides are refused rather than passed on, so
a flag cannot silently contradict the mode: the tag set, where the
result goes, the platforms, the Dockerfile, the metadata file and the
builder. Each has an input or an entry field of its own. So are the
flags that turn a build into something else (a check, an outline, the
help text): they can exit 0 having produced no image, which would
then be reported as built.
"""

from __future__ import annotations

import shlex
from typing import Any

from scripts.gha import ActionError

# Long flags the action owns, with the input or field that sets each.
_OWNED_LONG = {
    "--tag": "the mode and local_tag, repositories and tags",
    "--load": "mode",
    "--push": "mode",
    "--output": "mode",
    "--platform": "platforms",
    "--file": "the images entry's dockerfile",
    "--metadata-file": "mode: push",
    "--builder": "the buildx builder the action sets up",
}
# buildx's short aliases for owned flags; they may carry the value
# attached (-tname:tag).
_OWNED_SHORT = {"-t": "--tag", "-o": "--output", "-f": "--file"}
# Flags that run a frontend method instead of a build (--check is
# --call=check), or print help, and succeed without any image.
_NON_BUILDING = ("--call", "--check", "--help")


def _owner(flag: str) -> str | None:
    for long in _NON_BUILDING:
        if flag == long or flag.startswith(f"{long}="):
            return f"{long} does not build an image, and every build here must"
    if flag == "-h":
        return "-h (--help) does not build an image, and every build here must"
    for long, owner in _OWNED_LONG.items():
        if flag == long or flag.startswith(f"{long}="):
            return f"{long} is set by {owner}"
    for short, long in _OWNED_SHORT.items():
        if flag.startswith(short) and not flag.startswith("--"):
            return f"{short} ({long}) is set by {_OWNED_LONG[long]}"
    return None


def _refuse_owned(flags: list[str], origin: str) -> list[str]:
    for flag in flags:
        reason = _owner(flag)
        if reason:
            raise ActionError(f"{origin} cannot pass {flag}: {reason}")
    return flags


def shared_flags(raw: str) -> list[str]:
    """The build_flags input, split as a shell would split it."""
    try:
        flags = shlex.split(raw)
    except ValueError as err:
        raise ActionError(f"build_flags could not be parsed: {err}") from err
    return _refuse_owned(flags, "build_flags")


def image_flags(image: dict[str, Any]) -> list[str]:
    """An entry's own build_flags: a list, one argument per element."""
    value = image.get("build_flags")
    # null and false are absent, as jq's // reads the other fields.
    if value is None or value is False:
        return []
    if not isinstance(value, list) or not all(isinstance(flag, str) for flag in value):
        raise ActionError(
            f"images entry '{image.get('name')}' has build_flags that is not a list of strings"
        )
    return _refuse_owned(value, f"images entry '{image.get('name')}' build_flags")
