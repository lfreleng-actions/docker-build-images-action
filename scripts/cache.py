# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The opt-in BuildKit layer cache, held in the GitHub Actions cache.

``cache: gha`` adds ``--cache-from`` to every build, and with
``cache_write`` a ``--cache-to`` as well. Each image has its own scope,
so images do not evict one another's cache, and an optional
``cache_scope`` prefix keeps different lanes apart.

Not every builder can use it, so the action checks before the first
build and builds without the cache, saying why, on any builder that
cannot. The docker driver on Docker's classic image store, which
load-mode chains build on, cannot export a cache at all. The
docker-container builder can, as can the docker driver on the
containerd image store (https://docs.docker.com/build/cache/backends/).

The cache service's token can write the cache, so it never enters an
environment a build can read. buildx fills a bare ``--build-arg NAME``
and a ``--secret env=NAME`` from its own environment, and either could
come from the checkout; so the entry point takes the credentials out
of its environment before any docker call, and hands them to buildx
as attributes of the cache flags alone, which BuildKit's gha backend
reads (cache/remotecache/gha/gha.go). buildx fills those attributes
from ACTIONS_* variables only when they are absent (build/opt.go,
v0.37.2), so passing them explicitly takes the same path.

That keeps the token from the build's environment, not from the
runner: buildx itself holds it, and a checkout-controlled input that
makes buildx read a runner file, such as ``--secret
src=/proc/self/cmdline``, a Dockerfile path or a build context, could
recover it. On pull_request_target, where such inputs meet a token
that can write the base branch's cache, the action therefore never
fetches the credentials, and builds without the cache.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from scripts import docker, gha
from scripts.settings import Settings, untrusted_event

# Drivers whose BuildKit runs apart from the daemon's image store, and
# exports a cache wherever it runs.
_EXPORTING_DRIVERS = ("docker-container", "kubernetes", "remote")
# docker info's DriverStatus names this snapshotter on the containerd
# image store, whose docker driver exports a cache too.
_CONTAINERD = "io.containerd.snapshotter"
# Go's strconv.ParseBool true values, as buildx reads the v2 switch.
_GO_TRUE = ("1", "t", "T", "TRUE", "true", "True")


@dataclass(frozen=True, repr=False)
class Credentials:
    """The Actions cache service's token and URLs, as action.yaml read them."""

    token: str = ""
    results_url: str = ""
    cache_url: str = ""
    service_v2: str = ""

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    def attributes(self) -> str:
        """The cache flag attributes that carry these credentials.

        Empty when they are incomplete, or hold a character that would
        break buildx's CSV parsing of the flag.
        """
        url = self.cache_url or self.results_url
        if not (self.token and url):
            return ""
        attrs = {"token": self.token, "url": url}
        if self.service_v2 in _GO_TRUE and self.results_url:
            attrs |= {"url_v2": self.results_url, "version": "2"}
        if any(c in value for value in attrs.values() for c in ',"\n\r'):
            return ""
        return ",".join(f"{key}={value}" for key, value in attrs.items())


# The private names action.yaml passes the credentials under.
_ENVIRONMENT = {
    "token": "BUILD_IMAGES_CACHE_TOKEN",
    "results_url": "BUILD_IMAGES_CACHE_RESULTS_URL",
    "cache_url": "BUILD_IMAGES_CACHE_URL",
    "service_v2": "BUILD_IMAGES_CACHE_SERVICE_V2",
}


def take_credentials() -> Credentials:
    """Remove the cache credentials from the environment, and return them.

    Called before any docker call, so no build, and no build_command,
    inherits them.
    """
    return Credentials(
        **{field: os.environ.pop(name, "") for field, name in _ENVIRONMENT.items()}
    )


def _driver(cwd: str) -> str:
    """The driver of the builder the builds use; empty when unknown."""
    status, out = docker.query(["buildx", "inspect"], cwd)
    if status != 0:
        return ""
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "Driver":
            return value.strip()
    return ""


def _containerd_store(cwd: str) -> bool:
    status, out = docker.query(["info", "--format", "{{json .DriverStatus}}"], cwd)
    return status == 0 and _CONTAINERD in out


def _unsupported_reason(cwd: str) -> str:
    """Why this run's builder cannot use the cache; empty when it can."""
    driver = _driver(cwd)
    if driver in _EXPORTING_DRIVERS:
        return ""
    if driver == "docker":
        if _containerd_store(cwd):
            return ""
        return (
            "the docker driver on Docker's classic image store cannot export a "
            "cache; same-repository chains in mode: load need that driver"
        )
    if not driver:
        return "docker buildx inspect could not name the builder's driver"
    return f"the {driver} driver is not one this action supports with a cache"


def access(settings: Settings, cwd: str, credentials: Credentials) -> str:
    """The credential attributes this run's cache flags carry.

    Empty when the builds do not use the cache. Checked once, before
    the first build. A builder that cannot use the cache builds exactly
    as it would without one, with a notice; missing credentials are a
    warning, since the action itself supplies them.
    """
    if settings.cache != "gha":
        return ""
    event = untrusted_event()
    if event:
        gha.annotate(
            "notice",
            f"cache: gha is not used on {event} events, which build a pull "
            "request's inputs with a token that can write the base branch's "
            "cache. The images build without the cache",
        )
        return ""
    attributes = credentials.attributes()
    if not attributes:
        gha.annotate(
            "warning",
            "cache: gha has no usable Actions cache token and URL, so the images "
            "build without the cache",
        )
        return ""
    reason = _unsupported_reason(cwd)
    if reason:
        gha.annotate(
            "notice",
            f"cache: gha is not used: {reason}. The images build without the cache; "
            "see the README's Layer cache section",
        )
        return ""
    direction = "reads and writes" if settings.cache_write else "reads"
    gha.log(f"Layer cache: this run {direction} the GitHub Actions cache")
    return attributes


def scope(settings: Settings, name: str) -> str:
    """The image's cache scope: its name, after any cache_scope prefix.

    ':' cannot appear in an image name, so no prefix and name pair can
    produce another pair's scope.
    """
    return f"{settings.cache_scope}:{name}" if settings.cache_scope else name


def flags(settings: Settings, name: str, attributes: str) -> list[str]:
    """``--cache-from`` and, when writing, ``--cache-to`` for one image.

    ``attributes`` comes from access(); empty means no cache. mode=max
    keeps the intermediate stages too, where slow dependency installs
    tend to sit. ignore-error keeps a cache service outage from failing
    a build that itself succeeded.
    """
    if not attributes:
        return []
    source = f"type=gha,scope={scope(settings, name)},{attributes}"
    args = ["--cache-from", source]
    if settings.cache_write:
        args += ["--cache-to", f"{source},mode=max,ignore-error=true"]
    return args
