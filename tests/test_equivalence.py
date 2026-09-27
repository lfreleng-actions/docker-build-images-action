# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Differential tests: the action against the lanes' inline build loops.

The three docker-workflows build bodies are vendored verbatim in
tests/legacy. Each scenario runs one of them and the action against the
same scripted docker, and compares exit status, annotations, outputs,
and every docker call that changes state, in order. Reads may differ.

Four deliberate differences are accounted for, not hidden:

* the merge lane saves archives itself; the action leaves that to
  docker-save-images-action, as verify and release already do, so the
  merge body's ``docker save`` calls are set aside
* the release body lets ``set -e`` end the run on a failed build with
  no annotation and its log group still open; the action reports
  which build failed, closes the group, and still publishes outputs
* in a multi-platform push, later builds gain ``--build-context``
  arguments naming earlier pushed images by digest, so same-repository
  bases resolve inside the docker-container builder; the release body
  cannot build such a chain at all
* a single-platform Docker Hub push resolves its digest: docker reports
  RepoDigests in familiar form (onap/x, not docker.io/onap/x), so the
  release body's exact match fails every such release; that scenario
  is asserted on its own rather than compared

Outside these scenarios, inputs the lanes would fail on part-way are
refused before anything builds (test_action's ValidationTest), and a
failed load-mode alias fails its image where the lanes' set -e ends
the run.
"""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any

from tests.support import Run, SandboxTestCase, run_action, run_legacy

BASE = "FROM alpine:3\n"
CHILD = "ARG BASE=base:verify\nFROM ${BASE}\n"
FAILS = "FROM busybox\n# FAKE: fail\n"

CHAIN = {"base/Dockerfile": BASE, "child/Dockerfile": CHILD, "util/Dockerfile": BASE}


def images(*entries: Mapping[str, Any]) -> str:
    return json.dumps(list(entries))


def entry(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "context": extra.pop("context", name), **extra}


# name -> (files, images, namespace, path_prefix)
LOAD_SCENARIOS: dict[str, tuple[dict[str, str], str, str, str]] = {
    "single image": ({"app/Dockerfile": BASE}, images(entry("app")), "", "."),
    "chain in order": (
        CHAIN,
        images(entry("base"), entry("child"), entry("util")),
        "",
        ".",
    ),
    "chain out of order": (CHAIN, images(entry("child"), entry("base")), "", "."),
    "one image fails": (
        {**CHAIN, "util/Dockerfile": FAILS},
        images(entry("base"), entry("util"), entry("child")),
        "",
        ".",
    ),
    "every image fails": ({"a/Dockerfile": FAILS}, images(entry("a")), "", "."),
    "namespaced": (CHAIN, images(entry("base"), entry("child")), "onap", "."),
    "nested namespace": (CHAIN, images(entry("base")), "onap/sub", "."),
    "invalid namespace": (CHAIN, images(entry("base")), "ONAP", "."),
    "target, args, dockerfile": (
        {"x/Dockerfile.alt": BASE},
        images(
            entry(
                "x",
                dockerfile="x/Dockerfile.alt",
                target="runtime",
                build_args=["A=1", "B=two words"],
            )
        ),
        "",
        ".",
    ),
    "dockerfile false": (
        {"a/Dockerfile": BASE},
        images(entry("a", dockerfile=False)),
        "",
        ".",
    ),
    # Passed through as -f "", which buildx reads as <context>/Dockerfile.
    "dockerfile empty": (
        {"a/Dockerfile": BASE},
        images(entry("a", dockerfile="")),
        "",
        ".",
    ),
    "missing dockerfile": ({"a/x": "x"}, images(entry("a")), "", "."),
    "no images": ({}, "[]", "", "."),
    "sub-project prefix": (
        {"sub/app/Dockerfile": BASE},
        images(entry("app", context="app")),
        "",
        "sub",
    ),
}

LOAD_LANES = {
    # lane label -> (legacy lane, legacy extra env, action extra inputs)
    "verify permissive": (
        "verify",
        {"PERMIT_FAIL": "true"},
        {"build_permit_fail": "true"},
    ),
    "verify strict": ("verify", {"PERMIT_FAIL": "false"}, {}),
    "merge": ("merge", {}, {}),
}

# name -> (store, files, build_command, build_command_images, seed)
HATCH_SCENARIOS: dict[str, tuple[str, dict[str, str], str, str, dict[str, Any]]] = {
    "builds one (classic)": (
        "classic",
        {"out/Dockerfile": BASE},
        "docker buildx build --load -t out:v out",
        "",
        {},
    ),
    "pulls a base and builds (classic)": (
        "classic",
        {"out/Dockerfile": BASE},
        "docker pull alpine:3 && docker buildx build --load -t out:v out",
        "",
        {},
    ),
    "builds nothing, pulls a base (classic)": (
        "classic",
        {},
        "docker pull alpine:3",
        "",
        {},
    ),
    "produces nothing (classic)": ("classic", {}, "true", "", {}),
    "ordinary build (containerd)": (
        "containerd",
        {"out/Dockerfile": BASE},
        "docker buildx build --load -t out:v out",
        "",
        {},
    ),
    "pulls a base and builds (containerd)": (
        "containerd",
        {"out/Dockerfile": BASE},
        "docker pull alpine:3 && docker buildx build --load -t out:v out",
        "",
        {},
    ),
    "fails after leaving images (containerd)": (
        "containerd",
        {"out/Dockerfile": BASE},
        "docker buildx build --load -t out:v out && exit 3",
        "",
        {},
    ),
    "fails having built nothing": ("classic", {}, "exit 7", "", {}),
    "declared and built": (
        "containerd",
        {"out/Dockerfile": BASE},
        "docker buildx build --load -t onap/out:v1 out",
        "onap/out:v1",
        {},
    ),
    "declared, one missing": (
        "classic",
        {"out/Dockerfile": BASE},
        "docker buildx build --load -t a:v1 out",
        "a:v1, b:v1",
        {},
    ),
    "declared, stale tag cleared": (
        "classic",
        {},
        "true",
        "stale:v1",
        {"images": {"old": {"tags": ["stale:v1"], "digests": [], "origin": "build"}}},
    ),
    "declared, stale tag held by a container": (
        "classic",
        {},
        "true",
        "stale:v1",
        {
            "images": {"old": {"tags": ["stale:v1"], "digests": [], "origin": "build"}},
            "refuse_rm": ["stale:v1"],
        },
    ),
    "declared without a command": ("classic", {}, "", "a:v1", {}),
}

# PR #65's validation corpus for declared references.
DECLARED_CORPUS = [
    "onap/cps:latest",
    "localhost:5000/team/app:v1",
    "reg.io/a_b/c__d:v1",
    "localhost:5000/team/app",
    "app:",
    "app:v1:extra",
    "app@sha256:abc123",
    "app:v1 app:v1",
    "team//app:v1",
    "app/:v1",
    "team/_bad/app:v1",
    "team:port/app:v1",
    "bad-.example/team/app:v1",
    "bad..example/team/app:v1",
    "example./team/app:v1",
    ",",
    "a" * 248 + ":v1",
    "a" * 247 + ":v1",
    "reg.io/" + "a" * 255 + ":v1",
    "[2001:db8::1]:5000/app:v1",
    "2001:db8::1:5000/app:v1",
    "sha256:" + "0" * 64,
    "sha512:" + "0" * 64,
    "a_b.c/x:v1",
    "UPPER/x:v1",
    "app:v1\tother:v2\nthird:v3",
]


def strip_saves(run: Run) -> list[list[str]]:
    return [call for call in run.mutations if call[0] != "save"]


class Differential(SandboxTestCase):
    """Shared comparison."""

    def compare(
        self,
        lane: str,
        files: Mapping[str, str],
        legacy_env: Mapping[str, str],
        inputs: Mapping[str, str],
        store: str = "classic",
        seed: Mapping[str, Any] | None = None,
        ignore_annotations: tuple[str, ...] = (),
        action_env: Mapping[str, str] | None = None,
        closes_group: bool = False,
        adds_build_contexts: bool = False,
    ) -> None:
        runs = []
        for runner in ("legacy", "action"):
            self.tearDown()
            self.store = store
            self.setUp()
            self.sandbox.write(files)
            if seed:
                self.sandbox.seed(**seed)
            if runner == "legacy":
                runs.append(run_legacy(self.sandbox, lane, **legacy_env))
            else:
                runs.append(run_action(self.sandbox, env=action_env, **inputs))
        old, new = runs
        detail = f"\n--- legacy\n{old.stdout}\n--- action\n{new.stdout}"
        self.assertEqual(new.status, old.status, "exit status" + detail)
        annotations = [
            a for a in new.annotations if not a.startswith(ignore_annotations)
        ]
        if closes_group:
            self.assertEqual(
                annotations[-1:], ["::endgroup::"], "group closed" + detail
            )
            annotations = annotations[:-1]
        self.assertEqual(annotations, old.annotations, "annotations" + detail)
        mutations = new.mutations
        if adds_build_contexts:
            # The one deliberate addition: build contexts that let later
            # multi-platform builds resolve same-repository bases.
            mutations = [
                [a for a in call if not a.startswith("--build-context=")]
                for call in mutations
            ]
        self.assertEqual(mutations, strip_saves(old), "docker calls" + detail)
        for key, value in old.outputs.items():
            self.assertEqual(new.outputs.get(key), value, f"output {key}" + detail)


class LoadModeTest(Differential):
    """Load mode against the verify and merge bodies."""

    def test_native_loop(self) -> None:
        for scenario, (files, imgs, namespace, prefix) in LOAD_SCENARIOS.items():
            for label, (lane, legacy_extra, action_extra) in LOAD_LANES.items():
                with self.subTest(scenario=scenario, lane=label):
                    self.compare(
                        lane,
                        files,
                        {
                            "PATH_PREFIX": prefix,
                            "IMAGES_JSON": imgs,
                            "IMAGE_NAMESPACE": namespace,
                            **legacy_extra,
                        },
                        {
                            "path_prefix": prefix,
                            "images": imgs,
                            "image_namespace": namespace,
                            **action_extra,
                        },
                    )

    def test_escape_hatch(self) -> None:
        for scenario, (
            store,
            files,
            command,
            declared,
            seed,
        ) in HATCH_SCENARIOS.items():
            for label, (lane, legacy_extra, action_extra) in LOAD_LANES.items():
                with self.subTest(scenario=scenario, lane=label):
                    self.compare(
                        lane,
                        files,
                        {
                            "PATH_PREFIX": ".",
                            "IMAGES_JSON": "[]",
                            "BUILD_COMMAND": command,
                            "BUILD_COMMAND_IMAGES": declared,
                            **legacy_extra,
                        },
                        {
                            "images": "[]",
                            "build_command": command,
                            "build_command_images": declared,
                            **action_extra,
                        },
                        store=store,
                        seed=seed,
                    )

    def test_declared_reference_corpus(self) -> None:
        for declared in DECLARED_CORPUS:
            with self.subTest(declared=declared[:40]):
                self.compare(
                    "verify",
                    {},
                    {
                        "PATH_PREFIX": ".",
                        "BUILD_COMMAND": "true",
                        "BUILD_COMMAND_IMAGES": declared,
                    },
                    {"build_command": "true", "build_command_images": declared},
                )


# name -> (files, images, namespace, platforms, ghcr, dockerhub)
PUSH_SCENARIOS: dict[str, tuple[dict[str, str], str, str, str, bool, bool]] = {
    "GHCR, single platform": (
        CHAIN,
        images(entry("base"), entry("child")),
        "",
        "linux/amd64",
        True,
        False,
    ),
    "dry run, nothing resolved": (
        CHAIN,
        images(entry("base"), entry("child")),
        "onap",
        "linux/amd64",
        False,
        False,
    ),
    "multi-platform push": (
        CHAIN,
        images(entry("base"), entry("util")),
        "onap",
        "linux/amd64,linux/arm64",
        True,
        True,
    ),
    "multi-platform, nothing resolved": (
        CHAIN,
        images(entry("base")),
        "",
        "linux/amd64,linux/arm64",
        False,
        False,
    ),
    "foreign platform only": (
        CHAIN,
        images(entry("base")),
        "",
        "linux/arm64",
        True,
        False,
    ),
    "unusable namespace": (
        CHAIN,
        images(entry("base")),
        "-team",
        "linux/amd64",
        False,
        False,
    ),
    "target and args": (
        {"x/Dockerfile.alt": BASE},
        images(
            entry("x", dockerfile="x/Dockerfile.alt", target="t", build_args=["A=1"])
        ),
        "",
        "linux/amd64",
        True,
        False,
    ),
    "build fails": (
        {"a/Dockerfile": FAILS},
        images(entry("a")),
        "",
        "linux/amd64",
        True,
        False,
    ),
}


class PushModeTest(Differential):
    """Push mode against the release body."""

    @staticmethod
    def release_env(
        imgs: str, namespace: str, ghcr: bool, hub: bool, platforms: str
    ) -> dict[str, str]:
        return {
            "PATH_PREFIX": ".", "IMAGES_JSON": imgs, "IMAGE_NAMESPACE": namespace,
            "PLATFORMS": platforms, "TAG": "v1.2.3", "GHCR_PUBLISH": str(ghcr).lower(),
            "DOCKERHUB": str(hub).lower(), "OWNER": "MyOrg",
        }  # fmt: skip

    def test_docker_hub_single_platform_resolves_its_digest(self) -> None:
        # The fourth difference. docker reports RepoDigests in familiar
        # form (onap/base@..., not docker.io/onap/base@...), so the
        # release body's exact match finds nothing for Docker Hub and
        # fails a push that succeeded; the action matches familiar forms.
        imgs = images(entry("base"), entry("child"))
        self.sandbox.write(CHAIN)
        old = run_legacy(
            self.sandbox, "release", **self.release_env(imgs, "onap", True, True, "linux/amd64")
        )  # fmt: skip
        self.assertEqual(old.status, 1)
        self.assertEqual(
            old.annotations[-1],
            "::error::Failed to resolve digest for docker.io/onap/base:1.2.3 (got: )",
        )
        self.tearDown()
        self.setUp()
        self.sandbox.write(CHAIN)
        new = run_action(
            self.sandbox, env={"DOCKER_DEFAULT_PLATFORM": "linux/amd64"}, mode="push",
            images=imgs, image_namespace="onap", tags="1.2.3",
            repositories="ghcr.io/myorg,docker.io/onap",
        )  # fmt: skip
        self.assertEqual(new.status, 0, new.stdout)
        pushed = json.loads(new.outputs["pushed"])
        self.assertEqual(
            [(p["name"], p["image"]) for p in pushed],
            [("base", "ghcr.io/myorg/base"), ("base", "docker.io/onap/base"),
             ("child", "ghcr.io/myorg/child"), ("child", "docker.io/onap/child")],
        )  # fmt: skip
        # Each digest is the push's own, as the fake registry recorded it.
        for p in pushed:
            self.assertEqual(p["digest"], new.state["pushed"][f"{p['image']}:1.2.3"])

    def test_release_loop(self) -> None:
        for scenario, (
            files,
            imgs,
            namespace,
            platforms,
            ghcr,
            hub,
        ) in PUSH_SCENARIOS.items():
            repositories = [
                r
                for r, on in (("ghcr.io/myorg", ghcr), (f"docker.io/{namespace}", hub))
                if on
            ]
            with self.subTest(scenario=scenario):
                self.compare(
                    "release",
                    files,
                    self.release_env(imgs, namespace, ghcr, hub, platforms),
                    {
                        "mode": "push",
                        "images": imgs,
                        "image_namespace": namespace,
                        "platforms": platforms,
                        "repositories": ",".join(repositories),
                        "tags": "1.2.3",
                    },
                    ignore_annotations=("::error::Build failed for",),
                    # The release body takes linux/amd64 as the runner's
                    # platform; the action reads it from here.
                    action_env={"DOCKER_DEFAULT_PLATFORM": "linux/amd64"},
                    closes_group=scenario == "build fails",
                    adds_build_contexts="," in platforms and (ghcr or hub),
                )


if __name__ == "__main__":
    unittest.main()
