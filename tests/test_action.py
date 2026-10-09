# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Behaviour beyond the lanes: tunables, new outputs and validation.

Defaults are proven equivalent to the lanes' loops in test_equivalence;
everything here is what the action adds.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

from scripts.settings import IMAGES_ERROR
from tests.support import ROOT, Run, SandboxTestCase, run_action

BASE = "FROM alpine:3\n"
CHILD = "ARG BASE=base:verify\nFROM ${BASE}\n"
FAILS = "FROM busybox\n# FAKE: fail\n"
CHAIN = {"base/Dockerfile": BASE, "child/Dockerfile": CHILD, "util/Dockerfile": BASE}
AMD64 = {"DOCKER_DEFAULT_PLATFORM": "linux/amd64"}


def images(*names: str, **extra: dict[str, object]) -> str:
    return json.dumps([{"name": n, "context": n, **extra.get(n, {})} for n in names])


class OutputsTest(SandboxTestCase):
    """The per-image record and the failure lists."""

    def test_results_failed_and_counts(self) -> None:
        self.sandbox.write({**CHAIN, "util/Dockerfile": FAILS})
        run = run_action(
            self.sandbox,
            images=images("base", "util", "child"),
            build_permit_fail="true",
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            json.loads(run.outputs["results"]),
            [
                {"name": "base", "tag": "base:verify", "status": "built"},
                {"name": "util", "tag": "util:verify", "status": "failed"},
                {"name": "child", "tag": "child:verify", "status": "built"},
            ],
        )
        self.assertEqual(run.outputs["failed"], '["util:verify"]')
        self.assertEqual(run.outputs["failed_count"], "1")
        self.assertEqual(
            (run.outputs["pushed"], run.outputs["pushed_count"]), ("[]", "0")
        )
        self.assertIn("Failed **1** build(s):", run.summary)

    def test_matrix_output_is_accepted(self) -> None:
        self.sandbox.write(CHAIN)
        matrix = json.dumps(
            {
                "include": [
                    {"name": "base", "context": "base", "level": 0, "depends_on": []}
                ]
            }
        )
        run = run_action(self.sandbox, images=matrix)
        self.assertEqual((run.status, run.outputs["images"]), (0, '["base:verify"]'))

    def test_custom_local_tag(self) -> None:
        self.sandbox.write(CHAIN)
        run = run_action(self.sandbox, images=images("base"), local_tag="ci")
        self.assertEqual(run.outputs["images"], '["base:ci"]')

    def test_build_command_results_name_the_repository(self) -> None:
        # As load and push mode: name is the repository, tag the reference.
        self.sandbox.write({"out/Dockerfile": BASE})
        command = (
            "docker buildx build --load -t onap/out:v1 out && "
            "docker buildx build --load -t localhost:5000/team/app:v2 out"
        )
        declared = "onap/out:v1, localhost:5000/team/app:v2, missing:v1"
        run = run_action(
            self.sandbox, images="[]", build_command=command,
            build_command_images=declared, build_permit_fail="true",
        )  # fmt: skip
        self.assertEqual(
            json.loads(run.outputs["results"]),
            [
                {"name": "onap/out", "tag": "onap/out:v1", "status": "built"},
                {"name": "localhost:5000/team/app", "tag": "localhost:5000/team/app:v2",
                 "status": "built"},
                {"name": "missing", "tag": "missing:v1", "status": "failed"},
            ],
        )  # fmt: skip
        inferred = run_action(
            self.sandbox, images="[]",
            build_command="docker buildx build --load -t team/new:v3 out",
        )  # fmt: skip
        self.assertEqual(
            json.loads(inferred.outputs["results"]),
            [{"name": "team/new", "tag": "team/new:v3", "status": "built"}],
        )


class BuilderTest(SandboxTestCase):
    """Every build uses the builder the action set up."""

    def test_the_set_up_builder_outranks_a_selected_one(self) -> None:
        self.sandbox.write({**CHAIN, "out/Dockerfile": BASE})
        env = {"BUILD_IMAGES_BUILDER": "setup-one", "BUILDX_BUILDER": "stray"}
        run_action(self.sandbox, env=env, images=images("base", "child"))
        run = run_action(
            self.sandbox, env=env, images="[]",
            build_command="docker buildx build --load -t out:v out",
        )  # fmt: skip
        self.assertEqual(run.state["builders"], ["setup-one"] * 3)

    def test_a_callers_builder_stands_when_setup_is_skipped(self) -> None:
        self.sandbox.write(CHAIN)
        run = run_action(
            self.sandbox, env={"BUILDX_BUILDER": "mine", "BUILD_IMAGES_BUILDER": ""},
            images=images("base"),
        )  # fmt: skip
        self.assertEqual(run.state["builders"], ["mine"])

    def test_failed_alias_fails_that_image_only(self) -> None:
        self.sandbox.write(CHAIN)
        self.sandbox.seed(refuse_tag=["base:verify"])
        for permit, status in (("true", 0), ("false", 1)):
            with self.subTest(build_permit_fail=permit):
                run = run_action(
                    self.sandbox, images=images("base", "util"),
                    image_namespace="onap", build_permit_fail=permit,
                )  # fmt: skip
                self.assertEqual(run.status, status, run.stdout)
                self.assertIn(
                    "::error::Could not alias onap/base:verify as base:verify",
                    run.annotations,
                )
                self.assertEqual(
                    [
                        (r["name"], r["status"])
                        for r in json.loads(run.outputs["results"])
                    ],
                    [("base", "failed"), ("util", "built")],
                )
                self.assertEqual(run.outputs["failed"], '["onap/base:verify"]')
                self.assertEqual(run.outputs["images"], '["onap/util:verify"]')


class SkipDependentsTest(SandboxTestCase):
    """skip_dependents skips images whose same-repository base failed."""

    MATRIX = images("base", "child", "grandchild", "util", child={"depends_on": ["base"]},
                    grandchild={"depends_on": ["child"]})  # fmt: skip

    def setUp(self) -> None:
        super().setUp()
        self.sandbox.write(
            {
                **CHAIN,
                "base/Dockerfile": FAILS,
                "grandchild/Dockerfile": "FROM child:verify\n",
            }
        )

    def test_default_attempts_every_image(self) -> None:
        run = run_action(self.sandbox, images=self.MATRIX, build_permit_fail="true")
        builds = [c for c in run.mutations if c[:2] == ["buildx", "build"]]
        self.assertEqual(len(builds), 4)
        self.assertEqual(
            json.loads(run.outputs["failed"]),
            ["base:verify", "child:verify", "grandchild:verify"],
        )

    def test_skips_transitively(self) -> None:
        run = run_action(
            self.sandbox,
            images=self.MATRIX,
            build_permit_fail="true",
            skip_dependents="true",
        )
        builds = [
            c[c.index("-t") + 1] for c in run.mutations if c[:2] == ["buildx", "build"]
        ]
        self.assertEqual(builds, ["base:verify", "util:verify"])
        statuses = {r["name"]: r["status"] for r in json.loads(run.outputs["results"])}
        self.assertEqual(
            statuses,
            {
                "base": "failed",
                "child": "skipped",
                "grandchild": "skipped",
                "util": "built",
            },
        )
        self.assertIn(
            "::notice::Skipping child:verify: it builds from base, which did not build",
            run.annotations,
        )
        self.assertIn("Skipped **2** image(s)", run.summary)
        # Skipping is not failing: only the real failure gates.
        self.assertEqual(run.outputs["failed_count"], "1")


class PushTest(SandboxTestCase):
    """Push mode's generalisations of the release loop."""

    def test_several_repositories_and_tags(self) -> None:
        self.sandbox.write(CHAIN)
        run = run_action(
            self.sandbox,
            env=AMD64,
            mode="push",
            images=images("base"),
            repositories="ghcr.io/org\nnexus3.onap.org:10003/onap",
            tags="1.2.3, 1.2-STAGING-latest",
        )
        self.assertEqual(run.status, 0, run.stdout)
        pushes = [c[1] for c in run.mutations if c[0] == "push"]
        self.assertEqual(
            pushes,
            [
                "ghcr.io/org/base:1.2.3",
                "ghcr.io/org/base:1.2-STAGING-latest",
                "nexus3.onap.org:10003/onap/base:1.2.3",
                "nexus3.onap.org:10003/onap/base:1.2-STAGING-latest",
            ],
        )
        pushed = json.loads(run.outputs["pushed"])
        self.assertEqual(
            [p["image"] for p in pushed],
            ["ghcr.io/org/base", "nexus3.onap.org:10003/onap/base"],
        )
        self.assertTrue(all(p["digest"].startswith("sha256:") for p in pushed))

    def test_multi_platform_chain_resolves_through_build_contexts(self) -> None:
        # The docker-container builder cannot see daemon tags, so a later
        # FROM base:verify must come from the pushed digest. The legacy
        # release body fails here with 'pull access denied'.
        self.sandbox.write(CHAIN)
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images("base", "child"),
            repositories="ghcr.io/o", tags="1", platforms="linux/amd64,linux/arm64",
            image_namespace="onap",
        )  # fmt: skip
        self.assertEqual(run.status, 0, run.stdout)
        base, child = [c for c in run.mutations if c[:2] == ["buildx", "build"]]
        base_digest = json.loads(run.outputs["pushed"])[0]["digest"]
        source = f"docker-image://ghcr.io/o/base@{base_digest}"
        self.assertFalse(any(a.startswith("--build-context") for a in base))
        self.assertIn(f"--build-context=base:verify={source}", child)
        # The namespaced alias resolves too.
        self.assertIn(f"--build-context=onap/base:verify={source}", child)
        self.assertEqual(run.outputs["image_count"], "2")

    def test_failed_push_publishes_outputs_and_skips_the_rest(self) -> None:
        self.sandbox.write({**CHAIN, "child/Dockerfile": FAILS})
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images("base", "child", "util"),
            repositories="ghcr.io/o", tags="1",
        )  # fmt: skip
        self.assertEqual(run.status, 1)
        self.assertIn(
            "::error::Build failed for child:verify (exit 1)", run.annotations
        )
        statuses = [
            (r["name"], r["status"]) for r in json.loads(run.outputs["results"])
        ]
        self.assertEqual(
            statuses, [("base", "built"), ("child", "failed"), ("util", "skipped")]
        )
        self.assertEqual(run.outputs["failed"], '["child:verify"]')
        # What pushed before the failure is reported, not hidden.
        self.assertEqual(
            [p["image"] for p in json.loads(run.outputs["pushed"])], ["ghcr.io/o/base"]
        )
        self.assertEqual(
            len([c for c in run.mutations if c[:2] == ["buildx", "build"]]),
            2,
            "util never built",
        )
        self.assertIn("Not attempted after the failure: **1** image(s)", run.summary)

    def test_failed_alias_records_one_failure(self) -> None:
        # The namespaced alias runs before success is recorded, so a
        # refused tag leaves base failed once, never built and failed.
        self.sandbox.write(CHAIN)
        self.sandbox.seed(refuse_tag=["onap/base:verify"])
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images("base", "util"),
            repositories="ghcr.io/o", tags="1", image_namespace="onap",
        )  # fmt: skip
        self.assertEqual(run.status, 1)
        statuses = [
            (r["name"], r["status"]) for r in json.loads(run.outputs["results"])
        ]
        self.assertEqual(statuses, [("base", "failed"), ("util", "skipped")])
        self.assertEqual(run.outputs["images"], "[]")
        self.assertEqual(run.outputs["failed"], '["base:verify"]')

    def test_failed_digest_inspect_after_a_push_names_the_cause(self) -> None:
        self.sandbox.write(CHAIN)
        self.sandbox.seed(fail_inspect=True)
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images("base", "util"),
            repositories="ghcr.io/o", tags="1",
        )  # fmt: skip
        self.assertEqual(run.status, 1)
        self.assertIn(
            "::error::docker image inspect base:verify failed (exit 1): failed to "
            "connect to the docker API: connection refused",
            run.annotations,
        )
        self.assertFalse(any("Failed to resolve digest" in a for a in run.annotations))
        statuses = [
            (r["name"], r["status"]) for r in json.loads(run.outputs["results"])
        ]
        self.assertEqual(statuses, [("base", "failed"), ("util", "skipped")])

    def test_failed_pull_back_still_reports_the_push(self) -> None:
        # The manifest list is in the registry before the pull-back, so
        # a failure there must not hide it from pushed.
        self.sandbox.write(CHAIN)
        self.sandbox.seed(fail_pull=True)
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images("base", "util"),
            repositories="ghcr.io/o,docker.io/onap", tags="1",
            platforms="linux/amd64,linux/arm64",
        )  # fmt: skip
        self.assertEqual(run.status, 1)
        pushed = json.loads(run.outputs["pushed"])
        self.assertEqual(
            [p["image"] for p in pushed], ["ghcr.io/o/base", "docker.io/onap/base"]
        )
        manifest = run.state["pushed"]["ghcr.io/o/base:1"]
        self.assertEqual({p["digest"] for p in pushed}, {manifest})
        self.assertEqual(run.outputs["pushed_count"], "2")
        statuses = [
            (r["name"], r["status"]) for r in json.loads(run.outputs["results"])
        ]
        self.assertEqual(statuses, [("base", "failed"), ("util", "skipped")])

    def test_alias_length_excludes_a_registry_like_namespace(self) -> None:
        # reg.example.com/<245> is 261 characters, but docker counts
        # only the 245-character path, so the alias is valid.
        long = "a" * 245
        self.sandbox.write({f"{long}/Dockerfile": BASE})
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images(long),
            repositories="ghcr.io/o", tags="1", image_namespace="reg.example.com",
        )  # fmt: skip
        self.assertEqual(run.status, 0, run.stdout)
        self.assertIn(
            ["tag", f"{long}:verify", f"reg.example.com/{long}:verify"], run.mutations
        )
        self.assertFalse(
            any("Skipping the namespaced alias" in a for a in run.annotations)
        )

    def test_unusable_namespace_notice_names_the_local_tag(self) -> None:
        self.sandbox.write(CHAIN)
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images("base"),
            image_namespace="ONAP", local_tag="ci",
        )  # fmt: skip
        self.assertEqual(run.status, 0, run.stdout)
        self.assertIn(
            "::notice::image_namespace 'ONAP' is not a usable local reference prefix; "
            "skipping the namespaced :ci aliases",
            run.annotations,
        )

    def test_plan_chooses_driver_and_qemu(self) -> None:
        cases = [
            ({}, "docker", "false"),
            ({"mode": "push", "repositories": "ghcr.io/o", "tags": "1"}, "docker", "false"),
            ({"mode": "push", "platforms": "linux/amd64,linux/arm64", "repositories": "ghcr.io/o", "tags": "1"},
             "docker-container", "true"),
            ({"mode": "push", "platforms": "linux/amd64,linux/arm64"}, "docker", "true"),
            ({"mode": "push", "platforms": "linux/amd64"}, "docker", "false"),
        ]  # fmt: skip
        for inputs, driver, qemu in cases:
            with self.subTest(inputs=inputs):
                run = self.sandbox.invoke(
                    [sys.executable, "-I", str(ROOT / "entrypoint.py"), "plan"],
                    {**AMD64, **{f"INPUT_{k.upper()}": v for k, v in inputs.items()}},
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(
                    (run.outputs["driver"], run.outputs["qemu"]), (driver, qemu)
                )
                self.assertEqual(run.calls, [], "planning runs no docker")

    def test_plan_validates_before_any_setup(self) -> None:
        # The planner runs before QEMU and buildx, so these must fail
        # there rather than behind a setup error.
        self.sandbox.write(CHAIN)
        for inputs, message in (
            ({"path_prefix": "missing"}, "path_prefix 'missing' is not a directory"),
            ({"image_namespace": "ONAP"}, "Invalid image_namespace 'ONAP'"),
            ({"build_command_images": "a:v1"}, "build_command_images requires build_command"),
            ({"build_command": "true", "build_command_images": "bad ref"},
             "build_command_images entry 'bad' needs a tag on its final component"),
            ({"build_command": "true", "build_command_images": "a:v1 a:v1"},
             "build_command_images lists a:v1 more than once"),
            ({"images": '[{"name":"Bad","context":"base"}]'}, "entry Bad has a 'name'"),
        ):  # fmt: skip
            with self.subTest(inputs=inputs):
                run = self.sandbox.invoke(
                    [sys.executable, "-I", str(ROOT / "entrypoint.py"), "plan"],
                    {f"INPUT_{k.upper()}": v for k, v in inputs.items()},
                )
                self.assertEqual(run.status, 1)
                self.assertIn(message, run.annotations[0])
                self.assertEqual((run.calls, run.outputs), ([], {}))

    def test_plan_normalises_setup_buildx(self) -> None:
        # The setup step keys off the planner's output, so a typo fails
        # here instead of silently skipping setup.
        for value, expected in (("", "true"), ("True", "true"), ("FALSE", "false")):
            with self.subTest(value=value):
                run = self.sandbox.invoke(
                    [sys.executable, "-I", str(ROOT / "entrypoint.py"), "plan"],
                    {"INPUT_SETUP_BUILDX": value},
                )
                self.assertEqual(run.outputs["setup_buildx"], expected, run.stdout)
        run = self.sandbox.invoke(
            [sys.executable, "-I", str(ROOT / "entrypoint.py"), "plan"],
            {"INPUT_SETUP_BUILDX": "yes please"},
        )
        self.assertEqual(
            (run.status, run.annotations, run.outputs),
            (
                1,
                ["::error::setup_buildx must be 'true' or 'false', got 'yes please'"],
                {},
            ),
        )

    def test_plan_and_build_steps_read_the_same_inputs(self) -> None:
        steps = (ROOT / "action.yaml").read_text(encoding="utf-8").split("    - name:")
        env = {
            step.split("\n", 1)[0].strip(): set(re.findall(r"\n +(INPUT_\w+):", step))
            for step in steps
        }
        plan, build = env['"Plan the builder"'], env['"Build images"']
        self.assertEqual(plan, build)
        self.assertIn("INPUT_IMAGE_NAMESPACE", plan)


BY_DIGEST = "push-by-digest=true,name-canonical=true,push=true"


class PushByDigestTest(SandboxTestCase):
    """push_by_digest: untagged pushes, one digest record per registry."""

    def push(self, *names: str, **inputs: str) -> Run:
        self.sandbox.write(CHAIN)
        return run_action(
            self.sandbox, env=AMD64, mode="push", push_by_digest="true",
            images=images(*names), **inputs,
        )  # fmt: skip

    def builds(self, run: Run) -> list[list[str]]:
        return [c for c in run.mutations if c[:2] == ["buildx", "build"]]

    def assert_untagged(self, run: Run, names: tuple[str, ...]) -> None:
        """Every repository serves its pushed digest, and nothing is tagged."""
        pushed = json.loads(run.outputs["pushed"])
        self.assertEqual(run.outputs["pushed_count"], str(len(pushed)))
        self.assertEqual(run.state["pushed"], {}, "no tag was pushed")
        self.assertFalse(any(c[0] == "push" for c in run.mutations))
        served = run.state["manifests"]
        for entry in pushed:
            self.assertIn(entry["name"], names)
            self.assertNotIn("@", entry["image"])
            ref = f"{entry['image']}@{entry['digest']}"
            self.assertIn(ref, served)
            # Recorded only once that registry served it by digest.
            self.assertIn(["buildx", "imagetools", "inspect", "--raw", ref], run.calls)

    def test_single_platform_chain_pushes_untagged(self) -> None:
        run = self.push("base", "child", repositories="localhost:5000/a\nghcr.io/o")
        self.assertEqual(run.status, 0, run.stdout)
        base, child = self.builds(run)
        self.assertEqual(
            base[:7],
            [
                "buildx", "build", "--platform", "linux/amd64", "--output",
                f'type=image,"name=localhost:5000/a/base,ghcr.io/o/base",{BY_DIGEST}',
                "--provenance=false",
            ],
        )  # fmt: skip
        for build in (base, child):
            self.assertFalse({"-t", "--push", "--load"} & set(build), build)
        pushed = json.loads(run.outputs["pushed"])
        self.assertEqual(
            [(p["name"], p["image"]) for p in pushed],
            [
                ("base", "localhost:5000/a/base"),
                ("base", "ghcr.io/o/base"),
                ("child", "localhost:5000/a/child"),
                ("child", "ghcr.io/o/child"),
            ],
        )
        self.assert_untagged(run, ("base", "child"))
        # The chain resolves through the pushed base, and later jobs
        # get the pushed bits back under the local tag.
        source = f"localhost:5000/a/base@{pushed[0]['digest']}"
        self.assertIn(f"--build-context=base:verify=docker-image://{source}", child)
        self.assertIn(["pull", "--platform", "linux/amd64", source], run.mutations)
        self.assertIn(["tag", source, "base:verify"], run.mutations)
        self.assertEqual(run.outputs["images"], '["base:verify","child:verify"]')

    def test_multi_platform_pushes_an_untagged_index(self) -> None:
        run = self.push(
            "base", repositories="ghcr.io/o", platforms="linux/amd64,linux/arm64"
        )
        self.assertEqual(run.status, 0, run.stdout)
        (build,) = self.builds(run)
        self.assertEqual(
            build[2:6],
            [
                "--platform", "linux/amd64,linux/arm64", "--output",
                f'type=image,"name=ghcr.io/o/base",{BY_DIGEST}',
            ],
        )  # fmt: skip
        self.assert_untagged(run, ("base",))

    def test_a_registry_missing_the_digest_fails_the_image(self) -> None:
        self.sandbox.seed(drop_manifests=["ghcr.io/o/base"])
        run = self.push("base", "util", repositories="localhost:5000/a,ghcr.io/o")
        self.assertEqual(run.status, 1)
        pushed = json.loads(run.outputs["pushed"])
        # The registry that served it is reported; the one that did not
        # is named in the error.
        self.assertEqual([p["image"] for p in pushed], ["localhost:5000/a/base"])
        ref = f"ghcr.io/o/base@{pushed[0]['digest']}"
        self.assertIn(
            f"::error::docker buildx imagetools inspect {ref} failed (exit 1): "
            f"ERROR: {ref}: not found",
            run.annotations,
        )
        statuses = [
            (r["name"], r["status"]) for r in json.loads(run.outputs["results"])
        ]
        self.assertEqual(statuses, [("base", "failed"), ("util", "skipped")])

    def test_without_repositories_is_a_dry_run(self) -> None:
        run = self.push("base", "child")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertTrue(all("--load" in b for b in self.builds(run)))
        self.assertEqual(run.outputs["pushed"], "[]")
        self.assertNotIn("manifests", run.state)

    def test_plan_pushes_from_a_docker_container_builder(self) -> None:
        # The docker driver refuses push-by-digest (buildx build/opt.go),
        # so a single platform needs docker-container too, and no QEMU.
        cases = [
            ({"repositories": "ghcr.io/o"}, "docker-container", "false"),
            ({"repositories": "ghcr.io/o", "platforms": "linux/amd64,linux/arm64"},
             "docker-container", "true"),
            ({}, "docker", "false"),
        ]  # fmt: skip
        for inputs, driver, qemu in cases:
            with self.subTest(inputs=inputs):
                env = {"mode": "push", "push_by_digest": "true", **inputs}
                run = self.sandbox.invoke(
                    [sys.executable, "-I", str(ROOT / "entrypoint.py"), "plan"],
                    {**AMD64, **{f"INPUT_{k.upper()}": v for k, v in env.items()}},
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(
                    (run.outputs["driver"], run.outputs["qemu"]), (driver, qemu)
                )


class BuildFlagsTest(SandboxTestCase):
    """build_flags: raw buildx flags, shared and per image."""

    def builds(self, run: object) -> list[list[str]]:
        return [c for c in run.mutations if c[:2] == ["buildx", "build"]]  # type: ignore[attr-defined]

    def test_shared_and_per_image_flags_in_order(self) -> None:
        # The ODL case: --network=host for every image.
        self.sandbox.write(CHAIN)
        entries = json.dumps(
            [
                {
                    "name": "base",
                    "context": "base",
                    "target": "t",
                    "build_flags": ["--no-cache"],
                },
                {"name": "util", "context": "util"},
            ]
        )
        run = run_action(
            self.sandbox,
            images=entries,
            build_flags="--network=host --label 'org.x=a b'",
        )
        self.assertEqual(run.status, 0, run.stdout)
        base, util = self.builds(run)
        self.assertEqual(
            base,
            ["buildx", "build", "--load", "-f", "base/Dockerfile", "-t", "base:verify",
             "--target", "t", "--network=host", "--label", "org.x=a b", "--no-cache", "base"],
        )  # fmt: skip
        self.assertEqual(util[-4:], ["--network=host", "--label", "org.x=a b", "util"])

    def test_push_mode_passes_flags(self) -> None:
        self.sandbox.write(CHAIN)
        run = run_action(
            self.sandbox, env=AMD64, mode="push", images=images("base"),
            repositories="ghcr.io/o", tags="1", build_flags="--network=host",
        )  # fmt: skip
        self.assertEqual(run.status, 0, run.stdout)
        self.assertIn("--network=host", self.builds(run)[0])

    def test_owned_flags_are_refused(self) -> None:
        self.sandbox.write(CHAIN)
        for flag, reason in (
            ("--push", "--push is set by mode"),
            ("--load", "--load is set by mode"),
            ("--output=type=registry", "--output is set by mode"),
            ("-t other:tag", "-t (--tag) is set by"),
            ("-tother:tag", "-t (--tag) is set by"),
            ("--tag=x:1", "--tag is set by"),
            ("--platform linux/arm64", "--platform is set by platforms"),
            ("-f x/Dockerfile", "-f (--file) is set by the images entry's dockerfile"),
            ("--builder mine", "--builder is set by"),
            ("--call=check", "--call does not build an image"),
            ("--call outline", "--call does not build an image"),
            ("--check", "--check does not build an image"),
            ("--help", "--help does not build an image"),
            ("-h", "-h (--help) does not build an image"),
        ):
            with self.subTest(flag=flag):
                run = run_action(self.sandbox, images=images("base"), build_flags=flag)
                self.assertEqual(run.status, 1)
                self.assertIn(
                    f"build_flags cannot pass {flag.split()[0]}: {reason}",
                    run.annotations[0],
                )
                self.assertEqual(run.mutations, [])

    def test_per_image_flags_are_checked_before_building(self) -> None:
        self.sandbox.write(CHAIN)
        for flags, message in (
            (["--push"], "images entry 'util' build_flags cannot pass --push"),
            (
                "--no-cache",
                "images entry 'util' has build_flags that is not a list of strings",
            ),
            ([1], "images entry 'util' has build_flags that is not a list of strings"),
        ):
            with self.subTest(flags=flags):
                entries = json.dumps(
                    [
                        {"name": "base", "context": "base"},
                        {"name": "util", "context": "util", "build_flags": flags},
                    ]
                )
                run = run_action(self.sandbox, images=entries)
                self.assertEqual(run.status, 1)
                self.assertIn(message, run.annotations[0])
                self.assertEqual(run.mutations, [], "refused before the first build")

    def test_unbalanced_quotes(self) -> None:
        self.sandbox.write(CHAIN)
        run = run_action(
            self.sandbox, images=images("base"), build_flags="--label 'open"
        )
        self.assertEqual(run.status, 1)
        self.assertTrue(
            run.annotations[0].startswith("::error::build_flags could not be parsed")
        )


class ValidationTest(SandboxTestCase):
    """Misconfiguration fails before anything builds."""

    def test_rejected_inputs(self) -> None:
        self.sandbox.write(CHAIN)
        ok = images("base")
        cases = {
            "mode must be one of load, push; got 'publish'": {"mode": "publish"},
            "build_permit_fail is not available with mode: push": {
                "mode": "push", "build_permit_fail": "true"},
            "build_command is not available with mode: push": {"mode": "push", "build_command": "make"},
            "skip_dependents is not available with mode: push": {
                "mode": "push", "skip_dependents": "true"},
            "mode: push with repositories needs at least one tag": {
                "mode": "push", "repositories": "ghcr.io/o"},
            "repositories applies to mode: push only": {"repositories": "ghcr.io/o"},
            "tags applies to mode: push only": {"tags": "1"},
            "platforms applies to mode: push only": {"platforms": "linux/arm64"},
            # A non-breaking space is no separator (C-locale [:space:]),
            # so the entry stays whole and is refused.
            "tags entry 'bad\u00a0tag!' is not a valid Docker tag": {
                "mode": "push", "repositories": "ghcr.io/o", "tags": "ok,bad\u00a0tag!"},
            "local_tag 'no/slash' is not a valid Docker tag": {"local_tag": "no/slash"},
            "path_prefix '..' must be a path inside the workspace": {"path_prefix": ".."},
            "path_prefix 'missing' is not a directory": {"path_prefix": "missing"},
            "build_permit_fail must be 'true' or 'false', got 'maybe'": {"build_permit_fail": "maybe"},
        }  # fmt: skip
        for message, inputs in cases.items():
            with self.subTest(inputs=inputs):
                run = run_action(self.sandbox, env=AMD64, **{"images": ok, **inputs})
                self.assertEqual(
                    (run.status, run.annotations), (1, [f"::error::{message}"])
                )
                self.assertEqual(run.mutations, [], "nothing built")

    def test_push_by_digest_combinations(self) -> None:
        self.sandbox.write(CHAIN)
        cases = {
            "push_by_digest applies to mode: push only": {"push_by_digest": "true"},
            "tags cannot be set with push_by_digest: true, which pushes untagged": {
                "mode": "push", "push_by_digest": "true", "repositories": "ghcr.io/o",
                "tags": "1"},
            "push_by_digest must be 'true' or 'false', got 'maybe'": {
                "mode": "push", "push_by_digest": "maybe"},
        }  # fmt: skip
        for message, inputs in cases.items():
            for command in ([], ["plan"]):
                with self.subTest(inputs=inputs, command=command):
                    run = self.sandbox.invoke(
                        [sys.executable, "-I", str(ROOT / "entrypoint.py"), *command],
                        {
                            **AMD64,
                            "INPUT_IMAGES": images("base"),
                            **{f"INPUT_{k.upper()}": v for k, v in inputs.items()},
                        },
                    )
                    self.assertEqual(
                        (run.status, run.annotations), (1, [f"::error::{message}"])
                    )
                    self.assertEqual(run.calls, [], "nothing built")

    def test_repository_prefixes(self) -> None:
        self.sandbox.write(CHAIN)
        for prefix, valid in (
            ("ghcr.io/org", True),
            ("nexus3.onap.org:10003/onap", True),
            ("docker.io/onap/sub", True),
            ("onap", True),
            ("ghcr.io/Org", False),
            ("ghcr.io/org/", False),
            ("https://ghcr.io/org", False),
            # Label boundaries, as docker's domain grammar has them.
            ("bad-.example/org", False),
            ("bad..example/org", False),
            ("example./org", False),
            ("-bad.example/org", False),
            ("localhost:5000/org", True),
            ("reg-1.example.com:443/a/b", True),
            # A registry alone: the image name follows it.
            ("localhost:5000", True),
            ("ghcr.io", True),
            ("localhost", True),
            ("bad-.example:5000", False),
            ("example.:5000", False),
        ):
            with self.subTest(prefix=prefix):
                run = run_action(
                    self.sandbox, env=AMD64, mode="push", images=images("base"),
                    repositories=prefix, tags="1",
                )  # fmt: skip
                self.assertEqual(run.status == 0, valid, run.annotations)

    def test_entry_fields_are_checked_before_building(self) -> None:
        self.sandbox.write(CHAIN)
        push = {"mode": "push", "repositories": "ghcr.io/o", "tags": "1"}
        for field, value, problem in (
            ("build_args", "A=1", "has a 'build_args' that is not a list of strings"),
            ("build_args", [1], "has a 'build_args' that is not a list of strings"),
            ("depends_on", "base", "has a 'depends_on' that is not a list of strings"),
            ("dockerfile", ["Dockerfile"], "has a 'dockerfile' that is not a string"),
            ("target", 1, "has a 'target' that is not a string"),
            ("target", True, "has a 'target' that is not a string"),
            ("context", None, "needs a string 'context'"),
            (
                "context",
                "--help",
                "has a 'context' starting with '-', which buildx would read as a flag",
            ),
            (
                "context",
                "-",
                "has a 'context' starting with '-', which buildx would read as a flag",
            ),
        ):
            for mode in ({}, push):
                with self.subTest(field=field, value=value, mode=mode or "load"):
                    entries = json.dumps(
                        [
                            {"name": "base", "context": "base"},
                            {"name": "util", "context": "util", field: value},
                        ]
                    )
                    run = run_action(self.sandbox, env=AMD64, images=entries, **mode)
                    self.assertEqual(
                        (run.status, run.annotations),
                        (1, [f"::error::{IMAGES_ERROR}; entry util {problem}"]),
                    )
                    self.assertEqual(run.mutations, [], "refused before any build")
        run = run_action(self.sandbox, images='[{"context":"base"}]')
        self.assertEqual(
            run.annotations,
            [f"::error::{IMAGES_ERROR}; entry #1 needs a string 'name'"],
        )

    def test_null_and_false_fields_are_absent(self) -> None:
        # As jq's // reads them in the lanes.
        self.sandbox.write(CHAIN)
        entry = {
            "name": "base",
            "context": "base",
            "dockerfile": None,
            "target": False,
            "build_args": None,
            "depends_on": False,
            "build_flags": False,
        }
        run = run_action(self.sandbox, images=json.dumps([entry]))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["images"], '["base:verify"]')

    def test_image_names_are_checked_before_building(self) -> None:
        self.sandbox.write(CHAIN)
        push = {"mode": "push", "repositories": "ghcr.io/o", "tags": "1"}
        for name in ("Bad", "a:tag", "a//b", "-a", "a-", "a..b", "a/", ""):
            for mode in ({}, push):
                with self.subTest(name=name, mode=mode or "load"):
                    entries = json.dumps(
                        [
                            {"name": "base", "context": "base"},
                            {"name": name, "context": "util"},
                        ]
                    )
                    run = run_action(self.sandbox, env=AMD64, images=entries, **mode)
                    self.assertEqual(run.status, 1)
                    label = name or "#2"
                    self.assertIn(
                        f"; entry {label} has a 'name' that is not a valid Docker "
                        "repository name",
                        run.annotations[0],
                    )
                    self.assertEqual(run.mutations, [], "refused before any build")
        # Nested names are valid repository paths.
        self.sandbox.write({"sub/app/Dockerfile": BASE})
        run = run_action(
            self.sandbox, images=json.dumps([{"name": "sub/app", "context": "sub/app"}])
        )
        self.assertEqual(run.outputs["images"], '["sub/app:verify"]', run.stdout)

    def test_overlong_repository_paths_are_refused_up_front(self) -> None:
        self.sandbox.write(CHAIN)
        long = "a" * 240
        entries = json.dumps(
            [{"name": "base", "context": "base"}, {"name": long, "context": "util"}]
        )
        for inputs, full, length in (
            # The registry does not count; the namespace does.
            ({"mode": "push", "repositories": "ghcr.io/" + "o" * 20, "tags": "1"},
             f"ghcr.io/{'o' * 20}/{long}", 261),
            ({"image_namespace": "n" * 20}, f"{'n' * 20}/{long}", 261),
        ):  # fmt: skip
            with self.subTest(inputs=inputs):
                run = run_action(self.sandbox, env=AMD64, images=entries, **inputs)
                self.assertEqual(
                    run.annotations,
                    [
                        f"::error::images entry {long} resolves to {full}, a "
                        f"{length}-character repository path, over Docker's limit of 255"
                    ],
                )
                self.assertEqual(run.mutations, [])
        # A registry-only prefix leaves the path to the name alone.
        run = run_action(
            self.sandbox, env=AMD64, images=entries, mode="push",
            repositories="localhost:5000", tags="1",
        )  # fmt: skip
        self.assertEqual(run.status, 0, run.stdout)

    def test_docker_hub_single_names_count_as_library(self) -> None:
        # Checked on a real daemon: docker.io/ or index.docker.io/ plus
        # 248 characters is refused (library/ makes 256), 247 accepted.
        # The lanes count these without library/, so this sits outside
        # the compared corpus.
        for domain in ("docker.io", "index.docker.io"):
            for length, refused in ((248, True), (247, False)):
                ref = f"{domain}/{'a' * length}:v1"
                with self.subTest(domain=domain, length=length):
                    run = run_action(
                        self.sandbox, images="[]", build_command="true",
                        build_command_images=ref,
                    )  # fmt: skip
                    self.assertEqual(
                        f"::error::build_command_images entry '{ref}' resolves to a "
                        "256-character repository path, over Docker's limit of 255"
                        in run.annotations,
                        refused,
                    )
        self.sandbox.write(CHAIN)
        entries = json.dumps([{"name": "a" * 248, "context": "base"}])
        run = run_action(
            self.sandbox, env=AMD64, images=entries, mode="push",
            repositories="docker.io", tags="1",
        )  # fmt: skip
        self.assertEqual(run.annotations[0][:60], "::error::images entry " + "a" * 38)
        self.assertIn("a 256-character repository path", run.annotations[0])
        self.assertEqual(run.mutations, [])

    def test_load_namespace_components_are_checked_before_building(self) -> None:
        # The lanes' pattern admits these; docker refuses every one.
        self.sandbox.write(CHAIN)
        for namespace in ("-team", "team.", "a..b", ".", "a/./b", "a/b-"):
            message = (
                f"::error::Invalid image_namespace '{namespace}' (each '/'-separated "
                "component must be lowercase alphanumeric runs joined by '.', '_', "
                "'__' or '-')"
            )
            for command in ([], ["plan"]):
                with self.subTest(namespace=namespace, step=command or "build"):
                    run = self.sandbox.invoke(
                        [sys.executable, "-I", str(ROOT / "entrypoint.py"), *command],
                        {"INPUT_IMAGES": images("base"), "INPUT_IMAGE_NAMESPACE": namespace},
                    )  # fmt: skip
                    self.assertEqual((run.status, run.annotations), (1, [message]))
                    self.assertEqual(run.mutations, [])
        for namespace in ("a__b", "a-b", "onap/sub.team"):
            with self.subTest(namespace=namespace):
                run = run_action(
                    self.sandbox, images=images("base"), image_namespace=namespace
                )
                self.assertEqual(run.status, 0, run.stdout)

    def test_malformed_images(self) -> None:
        for raw in (
            "{",
            '{"name":"a"}',
            '[{"name":"a"}]',
            '["a"]',
            '[{"name":1,"context":"a"}]',
        ):
            with self.subTest(raw=raw):
                run = run_action(self.sandbox, images=raw)
                self.assertEqual(run.status, 1)
                self.assertTrue(
                    run.annotations[0].startswith("::error::images must be")
                )


class RobustnessTest(SandboxTestCase):
    """Unexpected failures still end with an annotation."""

    def test_familiar_names_match_docker(self) -> None:
        # Each pair checked against a real daemon's RepoTags.
        from scripts.refs import familiar

        for given, shown in (
            ("docker.io/onap/base", "onap/base"),
            ("index.docker.io/onap/base", "onap/base"),
            ("docker.io/library/base", "base"),
            ("library/base", "base"),
            ("docker.io/library/a/b", "library/a/b"),
            ("ghcr.io/org/base", "ghcr.io/org/base"),
            ("localhost:5000/base", "localhost:5000/base"),
        ):
            with self.subTest(given=given):
                self.assertEqual(familiar(given), shown)

    def test_failed_inspect_stops_before_the_command(self) -> None:
        # Read as absent, it would leave a stale declared tag uncleared
        # for a recovered daemon to report as newly built.
        self.sandbox.seed(
            images={"old": {"tags": ["stale:v1"], "digests": [], "origin": "build"}},
            fail_inspect=True,
        )
        run = run_action(
            self.sandbox, images="[]", build_command="touch ran",
            build_command_images="stale:v1",
        )  # fmt: skip
        self.assertEqual(run.status, 1)
        self.assertEqual(
            run.annotations,
            [
                "::error::docker image inspect stale:v1 failed (exit 1): failed to "
                "connect to the docker API: connection refused"
            ],
        )
        self.assertEqual(run.mutations, [])
        self.assertFalse((self.sandbox.workspace / "ran").exists())

    def test_failed_digest_inspect_is_an_error(self) -> None:
        # Read from empty output, a built image would pass as a pulled
        # base and drop silently from the results.
        self.sandbox.write({"out/Dockerfile": BASE})
        self.sandbox.seed(fail_inspect=True)
        run = run_action(
            self.sandbox, images="[]",
            build_command="docker buildx build --load -t out:v out",
        )  # fmt: skip
        self.assertEqual(run.status, 1)
        self.assertEqual(
            run.annotations[-1],
            "::error::docker image inspect out:v failed (exit 1): failed to connect "
            "to the docker API: connection refused",
        )
        self.assertNotIn("results", run.outputs)

    def test_failed_image_listing_stops_before_the_command(self) -> None:
        # An empty snapshot would pass every pre-existing tag off as new.
        self.sandbox.seed(fail_ls=True)
        run = run_action(self.sandbox, images="[]", build_command="touch ran")
        self.assertEqual(run.status, 1)
        self.assertIn(
            "::error::docker image ls failed (exit 1); cannot list the images",
            run.annotations,
        )
        self.assertEqual(run.mutations, [])
        self.assertFalse((self.sandbox.workspace / "ran").exists())

    def test_internal_error_is_annotated(self) -> None:
        from scripts import build

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(build, "build", side_effect=RuntimeError("boom")),
            mock.patch.dict(
                os.environ, {"INPUT_IMAGES": "[]", "INPUT_PATH_PREFIX": "."}
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(build.main(), 1)
        self.assertIn(
            "::error::Internal error in docker-build-images-action: RuntimeError('boom')",
            out.getvalue(),
        )
        self.assertIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
