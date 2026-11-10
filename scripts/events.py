# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The daemon's image events around build_command.

A build, ``buildx build --load`` on the docker driver and a commit each
emit ``create`` for the new image ID, and ``docker load`` (how jib and
the Gradle plugins hand over their images) emits ``load``. A pull emits
only ``pull`` and a re-tag only ``tag``, so the IDs in ``create`` and
``load`` events during the command are the images it produced, on the
classic and the containerd image store alike.
"""

from __future__ import annotations

import queue
import subprocess
import tempfile
import threading
import time
import uuid
from typing import IO

from scripts import gha

PRODUCED = frozenset({"create", "load"})

# The command is bracketed by two marker volumes, created and removed at
# once. A client that connects late is replayed only the daemon's latest
# 256 events, so seeing the first marker proves the stream live before
# the command starts, and it then misses nothing however much the
# command does. The daemon logs an event before the API call that
# caused it returns, so the second marker, made once the command has
# exited, follows every event the command caused: an exact end, where
# waiting for a stream to fall quiet cannot tell a drained stream from
# a slow one. MARK_WAIT bounds each wait; past it the digest rule
# applies.
MARK_WAIT = 30.0

_FORMAT = "{{.Type}} {{.Action}} {{.Actor.ID}}"
_FALLBACK = "; inferring the images from registry digests instead"


def _command(since: str) -> list[str]:
    return [
        "docker",
        "events",
        "--since",
        since,
        "--format",
        _FORMAT,
        "--filter",
        "type=image",
        "--filter",
        "type=volume",
    ]


def _lost(detail: str) -> None:
    gha.annotate("notice", f"Lost the daemon's image events ({detail}){_FALLBACK}")


def _last_line(text: str) -> str:
    return (text.strip().splitlines()[-1:] or ["no error output"])[0]


def _produced(lines: list[str]) -> set[str]:
    produced: set[str] = set()
    for line in lines:
        kind, action, actor = (line.split(" ", 2) + ["", ""])[:3]
        if kind == "image" and action in PRODUCED and actor:
            produced.add(actor)
    return produced


def _volume(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "volume", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=MARK_WAIT,
        check=False,
    )


class Watch:
    """A ``docker events`` stream running in the background."""

    def __init__(self, proc: subprocess.Popen[str], stderr: IO[str], cwd: str) -> None:
        self._proc = proc
        # A file, not a pipe: a full pipe would block the stream on its
        # diagnostics before it exits, which looks like a quiet stream.
        self._stderr = stderr
        self._cwd = cwd
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @classmethod
    def start(cls, cwd: str) -> Watch | None:
        """Start watching, or return None after a notice if it cannot.

        Returns once the stream is live. ``--since`` replays the first
        marker should it be made before the stream connects.
        """
        since = f"{time.time():.6f}"
        stderr = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                _command(since),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
            )
        except OSError as error:
            stderr.close()
            gha.annotate(
                "notice",
                f"Could not watch the daemon's image events ({error}){_FALLBACK}",
            )
            return None
        watch = cls(proc, stderr, cwd)
        if watch._through_mark() is None:
            watch.close()
            return None
        return watch

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _mark(self) -> str | None:
        """Make a marker volume and return its name, or None after a notice."""
        name = f"docker-build-images-mark-{uuid.uuid4().hex}"
        try:
            made = _volume(["create", name], self._cwd)
        except subprocess.TimeoutExpired:
            _lost(f"docker volume create did not finish within {MARK_WAIT:g}s")
            return None
        except OSError as error:
            _lost(str(error))
            return None
        if made.returncode != 0:
            _lost(
                f"docker volume create exited {made.returncode}: "
                f"{_last_line(made.stderr)}"
            )
            return None
        try:
            # Its create event is all the stream needs.
            _volume(["rm", name], self._cwd)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return name

    def _through_mark(self) -> list[str] | None:
        """The lines up to a fresh marker, or None after a notice."""
        name = self._mark()
        if name is None:
            return None
        wanted = f"volume create {name}"
        lines: list[str] = []
        deadline = time.monotonic() + MARK_WAIT
        while True:
            try:
                line = self._lines.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                _lost(f"docker events did not show its marker within {MARK_WAIT:g}s")
                return None
            if line is None:
                # The stream only ends when stopped, so it died early.
                status = self._proc.wait()
                self._stderr.seek(0)
                _lost(
                    f"docker events exited {status}: {_last_line(self._stderr.read())}"
                )
                return None
            line = line.strip()
            if line == wanted:
                return lines
            lines.append(line)

    def stop(self) -> set[str] | None:
        """The image IDs created or loaded, or None if the events were lost.

        Call it once the command has exited. A lost stream is reported
        as a notice; it never fails the step.
        """
        lines = self._through_mark()
        self.close()
        return None if lines is None else _produced(lines)

    def close(self) -> None:
        """End the stream; safe to call more than once."""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._reader.join(timeout=5)
        for pipe in (self._proc.stdout, self._stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()
