# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Shared helpers: a sandbox with the fake docker, and both runners."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEGACY = ROOT / "tests" / "legacy"

# Lane -> (vendored body, the variables its env: block binds).
LANES = {
    "verify": (
        "build-verify.sh",
        (
            "PATH_PREFIX",
            "IMAGES_JSON",
            "IMAGE_NAMESPACE",
            "BUILD_COMMAND",
            "BUILD_COMMAND_IMAGES",
            "PERMIT_FAIL",
        ),
    ),
    "merge": (
        "build-merge.sh",
        (
            "PATH_PREFIX",
            "IMAGES_JSON",
            "IMAGE_NAMESPACE",
            "BUILD_COMMAND",
            "BUILD_COMMAND_IMAGES",
        ),
    ),
    "release": (
        "build-release.sh",
        (
            "PATH_PREFIX",
            "IMAGES_JSON",
            "IMAGE_NAMESPACE",
            "PLATFORMS",
            "TAG",
            "GHCR_PUBLISH",
            "DOCKERHUB",
            "OWNER",
        ),
    ),
}

# Calls that change state; reads (inspect, ls) may legitimately differ.
MUTATING = ("buildx", "build", "tag", "push", "pull", "save", "rm")


@dataclass
class Run:
    """The observable result of one invocation."""

    status: int
    outputs: dict[str, str]
    annotations: list[str]
    stdout: str
    summary: str
    calls: list[list[str]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def mutations(self) -> list[list[str]]:
        """State-changing docker calls, with scratch paths normalised."""
        found = []
        for call in self.calls:
            head = call[1] if call[:1] == ["image"] else call[0]
            if head not in MUTATING or call[:2] == ["buildx", "imagetools"]:
                continue
            normalised = []
            for arg in call:
                if arg.endswith(".json") and "meta" in arg:
                    arg = "<metadata-file>"
                normalised.append(arg)
            found.append(normalised)
        return found


def parse_outputs(text: str) -> dict[str, str]:
    """Parse GITHUB_OUTPUT in both the ``k=v`` and heredoc forms."""
    outputs: dict[str, str] = {}
    lines = iter(line.removesuffix("\r") for line in text.split("\n"))
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            body: list[str] = []
            for item in lines:
                if item == delimiter:
                    break
                body.append(item)
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


class Sandbox:
    """A workspace, a fake docker on PATH and its state."""

    def __init__(self, root: pathlib.Path, store: str = "classic") -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fakedocker.py"}" "$@"\n'
        )
        docker.chmod(0o755)
        self.state_file = root / "state.json"
        self.state_file.write_text(json.dumps({"store": store}))

    def write(self, files: Mapping[str, str]) -> None:
        """Create files (relative path -> content) in the workspace."""
        for relative, content in files.items():
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def seed(self, **state: Any) -> None:
        """Merge values into the fake docker's state before a run."""
        current = json.loads(self.state_file.read_text())
        current.update(state)
        self.state_file.write_text(json.dumps(current))

    def invoke(self, command: list[str], env: Mapping[str, str]) -> Run:
        """Run ``command`` in the workspace against the fake docker."""
        scratch = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        output, summary, log = scratch / "output", scratch / "summary", scratch / "log"
        for path in (output, summary, log):
            path.touch()
        full_env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "LC_ALL": "C",
            "HOME": str(self.root),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKEDOCKER_STATE": str(self.state_file),
            "FAKEDOCKER_LOG": str(log),
            **env,
        }
        proc = subprocess.run(
            command,
            cwd=self.workspace,
            env=full_env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Run(
            status=proc.returncode,
            outputs=parse_outputs(output.read_text(encoding="utf-8")),
            annotations=[
                line for line in proc.stdout.splitlines() if line.startswith("::")
            ],
            stdout=proc.stdout + proc.stderr,
            summary=summary.read_text(encoding="utf-8"),
            calls=[json.loads(line) for line in log.read_text().splitlines() if line],
            state=json.loads(self.state_file.read_text()),
        )


def run_legacy(sandbox: Sandbox, lane: str, **env: str) -> Run:
    """Run a lane's vendored build body, as the runner does."""
    script, names = LANES[lane]
    bound = {name: env.get(name, "") for name in names}
    return sandbox.invoke(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(LEGACY / script)],
        bound,
    )


def run_action(
    sandbox: Sandbox, env: Mapping[str, str] | None = None, **inputs: str
) -> Run:
    """Run the action's entry point exactly as action.yaml does.

    ``inputs`` become INPUT_* variables; ``env`` passes through as is.
    """
    full = {f"INPUT_{key.upper()}": value for key, value in inputs.items()}
    full.setdefault("INPUT_PATH_PREFIX", ".")
    full.update(env or {})
    return sandbox.invoke([sys.executable, "-I", str(ROOT / "entrypoint.py")], full)


class SandboxTestCase(unittest.TestCase):
    """A test case with a fresh sandbox per test."""

    store = "classic"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = Sandbox(pathlib.Path(self._tmp.name), self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()
