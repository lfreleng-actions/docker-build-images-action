# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Validation of declared image references (build_command_images).

A port of the docker-workflows verify and merge lanes' checks, message
for message, including their deliberate strictness. PR #65 of that
repository cross-checked these rules against ``docker tag``: no false
accepts, and three references docker takes that these refuse
(``a_b.c/x:v1``, ``reg_istry.io/a:v1``, ``UPPER/x:v1``). Refusing a
reference docker would take costs a declaration that never ran;
accepting one docker rejects spends a whole build first.
"""

from __future__ import annotations

import re

from scripts.gha import ActionError

_IPV6_HOST = r"\[[0-9A-Fa-f:]+\]"
_BODY = r"[A-Za-z0-9][A-Za-z0-9._/:-]*"
_WHOLE = re.compile(_BODY)
_WHOLE_IPV6 = re.compile(rf"{_IPV6_HOST}(:[0-9]+)?/{_BODY}")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
COMPONENT = re.compile(r"[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*")
# A repository path without a registry: '/'-separated components, the
# grammar for image names and namespaces alike.
PATH = re.compile(rf"{COMPONENT.pattern}(/{COMPONENT.pattern})*")
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
# Docker's domain grammar: dot-separated labels, each starting and
# ending alphanumeric with hyphens only inside, and an optional port.
REGISTRY = re.compile(rf"({_LABEL}([.]{_LABEL})*|{_IPV6_HOST})(:[0-9]+)?")
_COMPONENT = COMPONENT
_REGISTRY = REGISTRY
# The legacy splits with tr on ',' and [:space:], which in the C locale
# is exactly these; Python's \s would also take Unicode spaces.
_SEPARATORS = re.compile(r"[,\t\n\v\f\r ]+")

MAX_PATH = 255


def split_list(raw: str) -> list[str]:
    """Comma- or whitespace-separated items, empties dropped."""
    return [item for item in _SEPARATORS.split(raw) if item]


def _entry_error(declared: str, detail: str) -> ActionError:
    return ActionError(f"build_command_images entry '{declared}' {detail}")


def is_registry(component: str) -> bool:
    """docker's rule: a leading component with '.' or ':', or localhost."""
    return "." in component or ":" in component or component == "localhost"


_DOCKER_HUB = ("docker.io/", "index.docker.io/")


def familiar(repository: str) -> str:
    """A repository as docker prints it: Docker Hub's prefixes dropped.

    docker image inspect reports RepoTags and RepoDigests this way
    (reference.FamiliarName): docker.io/onap/x and index.docker.io/onap/x
    are onap/x, and docker.io/library/x and library/x are x, while
    library/a/b keeps its prefix.
    """
    for prefix in _DOCKER_HUB:
        if repository.startswith(prefix):
            repository = repository[len(prefix) :]
            break
    if repository.startswith("library/") and repository.count("/") == 1:
        return repository[len("library/") :]
    return repository


def repository_path(name: str) -> str:
    """The part of a repository name docker caps at 255 characters.

    The registry is excluded, and a single-component Docker Hub name
    normalises to library/<name> first, whether the domain is implied
    or explicit: docker.io/x and index.docker.io/x are library/x too,
    as a real daemon confirms at the 255/256 boundary.
    """
    first, sep, rest = name.partition("/")
    if sep and is_registry(first):
        if first in ("docker.io", "index.docker.io") and "/" not in rest:
            return f"library/{rest}"
        return rest
    return name if sep else f"library/{name}"


def validate_reference(declared: str) -> None:
    """Raise ActionError unless ``declared`` is a usable name:tag."""
    if not (_WHOLE.fullmatch(declared) or _WHOLE_IPV6.fullmatch(declared)):
        raise ActionError(
            f"Invalid build_command_images entry: {declared} (expected name:tag, no digest)"
        )
    body, sep, tag = declared.rpartition(":")
    # Cutting at the last colon finds a tag only when the final
    # component carries one; on localhost:5000/team/app it finds the
    # registry port instead.
    if not sep or "/" in tag:
        raise _entry_error(declared, "needs a tag on its final component")
    if not _TAG.fullmatch(tag):
        raise _entry_error(declared, f"has an invalid tag '{tag}'")
    # sha256:<hex> is an image id to docker, which image inspect would
    # resolve against any image already on the daemon.
    if body == "sha256":
        raise _entry_error(declared, "names an image by id, not a repository and tag")
    if body.endswith("/"):
        raise _entry_error(declared, "has an empty path component")
    parts = body.split("/")
    for index, part in enumerate(parts):
        if not part:
            raise _entry_error(declared, "has an empty path component")
        # docker's rule: a leading component with a '.' or ':', or
        # named localhost, is a registry.
        if index == 0 and len(parts) > 1 and is_registry(part):
            if not _REGISTRY.fullmatch(part):
                raise _entry_error(declared, f"has an invalid registry '{part}'")
        elif not _COMPONENT.fullmatch(part):
            raise _entry_error(declared, f"has an invalid path component '{part}'")
    path = repository_path(body)
    if len(path) > MAX_PATH:
        raise _entry_error(
            declared,
            f"resolves to a {len(path)}-character repository path, over Docker's limit of 255",
        )


def declared_images(raw: str) -> list[str]:
    """Parse and validate build_command_images, in order."""
    declared: list[str] = []
    for item in split_list(raw):
        validate_reference(item)
        if item in declared:
            raise ActionError(f"build_command_images lists {item} more than once")
        declared.append(item)
    # A value of only separators would fall through to inference, the
    # very thing the caller supplied the input to replace.
    if raw and not declared:
        raise ActionError(
            "build_command_images is set but names no references; list the images "
            "the command produces, or remove the input to enumerate them by inference"
        )
    return declared
