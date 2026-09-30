# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Entry point: plan the builder, or build and report.

``plan`` decides what the composite action sets up before building.
The docker driver builds against the daemon's own image store, which
same-repository chains need: under the docker-container driver an
image --load'ed by one build is invisible to the next build's FROM.
Only a multi-platform push needs docker-container, since the docker
driver cannot produce a manifest list, and QEMU for foreign platforms.
"""

from __future__ import annotations

import os
import re
import sys
import traceback

from scripts import gha, hatch, native, push
from scripts.gha import ActionError
from scripts.outcome import Outcome, compact
from scripts.refs import PATH, declared_images
from scripts.settings import Settings

# The verify and merge lanes' check on the namespace in load mode, where
# it prefixes every tag, kept so their message stays identical. It
# admits components docker refuses ('-team', 'a..b', '.'), so the
# component grammar follows, before anything builds.
_LOAD_NAMESPACE = r"[a-z0-9._-]+(/[a-z0-9._-]+)*"


def _workdir(settings: Settings) -> str:
    workspace = os.path.realpath(os.getcwd())
    path = os.path.realpath(os.path.join(workspace, settings.path_prefix))
    if (
        os.path.isabs(settings.path_prefix)
        or os.path.commonpath([workspace, path]) != workspace
    ):
        raise ActionError(
            f"path_prefix '{settings.path_prefix}' must be a path inside the workspace"
        )
    if not os.path.isdir(path):
        raise ActionError(f"path_prefix '{settings.path_prefix}' is not a directory")
    return path


def _multi(settings: Settings) -> bool:
    return settings.mode == "push" and settings.platforms not in (
        (),
        (push.native_platform(),),
    )


def plan(settings: Settings) -> dict[str, str]:
    """The buildx driver, whether to set it up, and whether QEMU is needed."""
    multi = _multi(settings)
    container = multi and bool(settings.repositories)
    return {
        "driver": "docker-container" if container else "docker",
        "qemu": str(multi).lower(),
        "setup_buildx": str(settings.setup_buildx).lower(),
    }


def _check_load(settings: Settings) -> None:
    namespace = settings.image_namespace
    if namespace and not re.fullmatch(_LOAD_NAMESPACE, namespace):
        raise ActionError(
            f"Invalid image_namespace '{namespace}' (lowercase path components without "
            "leading/trailing/consecutive slashes)"
        )
    if namespace and not PATH.fullmatch(namespace):
        raise ActionError(
            f"Invalid image_namespace '{namespace}' (each '/'-separated component must "
            "be lowercase alphanumeric runs joined by '.', '_', '__' or '-')"
        )
    # build_command_images names what build_command produces; ignored,
    # it would leave a caller believing they had declared their images.
    if settings.build_command_images and not settings.build_command:
        raise ActionError(
            "build_command_images requires build_command; it names the images that "
            "command produces and has no meaning without it"
        )


def preflight(settings: Settings) -> str:
    """Every Docker-free check; returns the working directory.

    Planning runs it too, so a misconfiguration fails before QEMU or
    buildx is set up, and a setup failure cannot mask it.
    """
    cwd = _workdir(settings)
    if settings.mode != "push":
        _check_load(settings)
        if settings.build_command_images:
            declared_images(settings.build_command_images)
    return cwd


def build(settings: Settings, outcome: Outcome) -> None:
    """Build every image as the mode directs."""
    cwd = preflight(settings)
    if settings.mode == "push":
        push.build_all(settings, cwd, outcome)
        return
    if settings.build_command:
        hatch.run(settings.build_command, settings.build_command_images, cwd, outcome)
    else:
        native.build_all(settings, cwd, outcome)


def _bullets(items: list[str], empty: str) -> list[str]:
    return [f"- {item}" for item in items] or [f"- {empty}"]


def _summary(settings: Settings, outcome: Outcome) -> str:
    built = len(outcome.built)
    if settings.mode == "push":
        lines = [
            "## Docker Build/Push",
            "",
            f"Built **{built}** image(s); pushed **{len(outcome.pushed)}** reference(s):",
            "",
            *(
                [f"- `{p['image']}@{p['digest']}`" for p in outcome.pushed]
                or ["- (nothing pushed)"]
            ),
            "",
        ]
        if outcome.failures:
            lines += [f"Failed **{len(outcome.failures)}** build(s):", ""]
            lines += _bullets(outcome.failures, "") + [""]
        if outcome.skipped:
            lines += [
                f"Not attempted after the failure: **{len(outcome.skipped)}** image(s):",
                "",
            ]
            lines += _bullets(outcome.skipped, "") + [""]
        return "\n".join(lines) + "\n"
    lines = ["## Docker Build", "", f"Built **{built}** image(s):", ""]
    lines += _bullets(outcome.built, "(none)") + [""]
    if outcome.failures:
        lines += [f"Failed **{len(outcome.failures)}** build(s):", ""]
        lines += _bullets(outcome.failures, "") + [""]
    if outcome.skipped:
        lines += [
            f"Skipped **{len(outcome.skipped)}** image(s) whose base did not build:",
            "",
        ]
        lines += _bullets(outcome.skipped, "") + [""]
    return "\n".join(lines) + "\n"


def publish(settings: Settings, outcome: Outcome) -> None:
    """Outputs, log lines and the summary; before any gate, so whatever
    did build stays visible either way."""
    built = compact(outcome.built)
    gha.log(f"Built {len(outcome.built)} image(s): {built}")
    if settings.mode == "push":
        gha.log(f"Pushed {len(outcome.pushed)} image reference(s)")
    gha.set_outputs(
        {
            "images": built,
            "image_count": str(len(outcome.built)),
            # docker-save-images-action takes a space-separated list; a
            # reference cannot hold whitespace, so the join is safe.
            "images_list": " ".join(outcome.built),
            "failed": compact(outcome.failures),
            "failed_count": str(len(outcome.failures)),
            "results": compact(outcome.results),
            "pushed": compact(outcome.pushed),
            "pushed_count": str(len(outcome.pushed)),
        }
    )
    if settings.summary:
        gha.append_summary(_summary(settings, outcome))


def gate(settings: Settings, outcome: Outcome) -> int:
    """Fail on build failures, unless build_permit_fail allows them."""
    if not outcome.failures:
        return 0
    if not settings.permit_fail:
        return 1
    gha.annotate(
        "warning",
        f"{len(outcome.failures)} build(s) failed; permitted by build_permit_fail",
    )
    if settings.summary:
        gha.append_summary("⚠️ Build failures permitted by build_permit_fail\n")
    return 0


def _pin_builder() -> None:
    """Build with the builder this action set up, when it set one up.

    setup-buildx does not select a docker-driver builder, so a
    docker-container builder a caller selected earlier would otherwise
    take these builds, and same-repository chains could not see each
    other's images. BUILDX_BUILDER outranks that selection, reaches
    build_command's tooling too, and is left alone when setup was
    skipped, so a caller's own choice stands.
    """
    builder = os.environ.get("BUILD_IMAGES_BUILDER", "")
    if builder:
        os.environ["BUILDX_BUILDER"] = builder


def main() -> int:
    """Run the action; returns the process exit status."""
    try:
        settings = Settings.from_env()
        if sys.argv[1:] == ["plan"]:
            preflight(settings)
            gha.set_outputs(plan(settings))
            return 0
        _pin_builder()
        outcome = Outcome()
        build(settings, outcome)
        publish(settings, outcome)
        return gate(settings, outcome)
    except ActionError as err:
        gha.annotate("error", str(err))
        return 1
    except Exception as err:
        # Every expected failure is an ActionError; anything else is a
        # bug, but it must still fail the step with an annotation.
        traceback.print_exc()
        gha.annotate("error", f"Internal error in docker-build-images-action: {err!r}")
        return 1
