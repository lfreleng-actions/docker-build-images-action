# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The docker CLI, as the build steps call it.

Builds and pushes stream their output to the job log, inside a group;
queries capture theirs. Everything resolves ``docker`` from PATH, so
tests substitute a scripted stand-in.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Sequence

from scripts import gha
from scripts.gha import ActionError


def run(args: Sequence[str], cwd: str, group: str = "") -> int:
    """Run docker with output to the log; returns the exit status."""
    if group:
        gha.group(group)
    sys.stdout.flush()
    status = subprocess.run(["docker", *args], cwd=cwd, check=False).returncode
    sys.stdout.flush()
    if group:
        gha.endgroup()
    return status


def _capture(args: Sequence[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def query(args: Sequence[str], cwd: str) -> tuple[int, str]:
    """Run docker for its output; returns (status, stdout)."""
    proc = _capture(args, cwd)
    return proc.returncode, proc.stdout


def _inspect_failed(ref: str, proc: subprocess.CompletedProcess[str]) -> ActionError:
    detail = proc.stderr.strip().splitlines()[-1:] or ["no error output"]
    return ActionError(
        f"docker image inspect {ref} failed (exit {proc.returncode}): {detail[0]}"
    )


def exists(ref: str, cwd: str) -> bool:
    """Whether ``ref`` names an image in the local store.

    Only docker's 'No such image' means absent; it exits 1 for an
    unreachable daemon too. Read as absent, that would skip clearing a
    stale declared tag, which a recovered daemon then reports as newly
    built, so any other failure is an error.
    """
    proc = _capture(["image", "inspect", ref], cwd)
    if proc.returncode == 0:
        return True
    if "No such image" in proc.stderr:
        return False
    raise _inspect_failed(ref, proc)


def local_tags(cwd: str) -> list[str]:
    """Sorted, de-duplicated repository:tag names, as the lanes list them.

    A failed listing is an error, not an empty snapshot: taken before a
    build_command as an empty list, every pre-existing tag would read as
    newly built afterwards, and pass stale images downstream. The lanes'
    pipeline stops on it the same way, under pipefail.
    """
    status, out = query(["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], cwd)
    if status != 0:
        raise ActionError(
            f"docker image ls failed (exit {status}); cannot list the images"
        )
    return sorted({line for line in out.split("\n") if line})


def repo_digests(ref: str, cwd: str) -> list[str]:
    """``repo@sha256:...`` entries the store records for ``ref``."""
    proc = _capture(
        [
            "image",
            "inspect",
            "--format",
            "{{range .RepoDigests}}{{println .}}{{end}}",
            ref,
        ],
        cwd,
    )
    if proc.returncode != 0:
        raise _inspect_failed(ref, proc)
    return [line for line in proc.stdout.split("\n") if line]


def served_digest(ref: str, cwd: str) -> str:
    """The digest of the manifest a registry serves for ``ref``.

    The hash of the raw bytes, which is what a content-addressed
    registry stores the manifest under. Anything short of a served
    manifest is an error, naming docker's own reason.
    """
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        lines = proc.stderr.decode(errors="replace").strip().splitlines()
        raise ActionError(
            f"docker buildx imagetools inspect {ref} failed (exit {proc.returncode}): "
            f"{(lines[-1:] or ['no error output'])[0]}"
        )
    return "sha256:" + hashlib.sha256(proc.stdout).hexdigest()


def digest_count(ref: str, cwd: str) -> str:
    """How many registry digests ``ref`` carries, as docker prints it.

    A failed inspect is an error, as it is under the lanes' set -e:
    classified from empty output, a built image would read as a pulled
    base and could drop silently from the results.
    """
    proc = _capture(["image", "inspect", "--format", "{{len .RepoDigests}}", ref], cwd)
    if proc.returncode != 0:
        raise _inspect_failed(ref, proc)
    return proc.stdout.strip()
