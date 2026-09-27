# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A scripted stand-in for the docker CLI, for offline tests.

Installed on PATH as ``docker``, it models what the build steps rely
on and nothing more: an image store of local tags, registry digests
recorded by pushes and pulls, and which store the daemon uses.

* classic store: locally built images carry no RepoDigests, pulled
  ones do (what GitHub-hosted runners use)
* containerd store: every image carries a digest, so the digest no
  longer tells a local build from a pull

A Dockerfile controls its own build: a line ``# FAKE: fail`` fails
it, and a ``FROM`` naming a ``:verify`` tag fails unless that tag is
already in the store, the way a same-repository chain fails when its
base was not built first. ``ARG`` defaults and ``--build-arg`` values
substitute into ``FROM`` first.

State lives in the JSON file ``$FAKEDOCKER_STATE``; every invocation
appends its argv to ``$FAKEDOCKER_LOG``, one JSON array per line.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

State = dict[str, Any]


def _load() -> State:
    path = Path(os.environ["FAKEDOCKER_STATE"])
    state: State = json.loads(path.read_text()) if path.exists() else {}
    state.setdefault("store", "classic")
    state.setdefault("images", {})
    state.setdefault("pushed", {})
    state.setdefault("refuse_rm", [])
    state.setdefault("counter", 0)
    return state


def _save(state: State) -> None:
    Path(os.environ["FAKEDOCKER_STATE"]).write_text(json.dumps(state, indent=1))


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def repository(ref: str) -> str:
    """A reference without its tag or digest."""
    if "@" in ref:
        return ref.split("@", 1)[0]
    if ref.rfind(":") > ref.rfind("/"):
        return ref[: ref.rfind(":")]
    return ref


def familiar(ref: str) -> str:
    """``ref`` as docker stores and prints it (reference.FamiliarName).

    Docker Hub's docker.io/ and index.docker.io/ prefixes are dropped,
    and library/ too for a single-component name; verified against a
    real daemon: docker.io/library/a/b stays library/a/b.
    """
    repo = repository(ref)
    suffix = ref[len(repo) :]
    for prefix in ("docker.io/", "index.docker.io/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix) :]
            break
    if repo.startswith("library/") and repo.count("/") == 1:
        repo = repo[len("library/") :]
    return repo + suffix


def _find(state: State, ref: str) -> str | None:
    ref = familiar(ref)
    for image_id, image in state["images"].items():
        if ref in image["tags"] or ref in image["digests"]:
            return image_id
    return None


def _untag(state: State, ref: str) -> None:
    ref = familiar(ref)
    for image_id in list(state["images"]):
        image = state["images"][image_id]
        if ref in image["tags"]:
            image["tags"].remove(ref)
            if not image["tags"] and not image["digests"]:
                del state["images"][image_id]


def _new_image(state: State, tags: list[str], digests: list[str], origin: str) -> str:
    state["counter"] += 1
    image_id = _hash(origin, str(state["counter"]), *tags)[:12]
    for tag in tags:
        _untag(state, tag)
    state["images"][image_id] = {
        "tags": [familiar(tag) for tag in tags],
        "digests": [familiar(digest) for digest in digests],
        "origin": origin,
    }
    return image_id


def _fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def _resolve_from(dockerfile: str, build_args: dict[str, str]) -> list[str]:
    variables: dict[str, str] = {}
    refs = []
    for line in dockerfile.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0].upper() == "ARG" and len(words) > 1:
            name, _, default = words[1].partition("=")
            variables[name] = build_args.get(name, default)
        elif words[0].upper() == "FROM" and len(words) > 1:
            ref = re.sub(
                r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
                lambda m: variables.get(m.group(1), ""),
                words[1],
            )
            refs.append(ref)
    return refs


def _buildx(state: State, args: list[str]) -> int:
    # As real buildx: help anywhere, even where a context is expected,
    # prints usage and exits 0 having built nothing.
    if "--help" in args or "-h" in args:
        print("Usage:  docker buildx build [OPTIONS] PATH | URL | -")
        return 0
    # The builder each build would use, for tests of builder selection.
    state.setdefault("builders", []).append(os.environ.get("BUILDX_BUILDER", ""))
    tags: list[str] = []
    build_args: dict[str, str] = {}
    contexts: dict[str, str] = {}
    dockerfile = ""
    metadata = ""
    push = False
    positional: list[str] = []
    it = iter(args)
    for arg in it:
        if arg in ("-f", "--file"):
            dockerfile = next(it)
        elif arg in ("-t", "--tag"):
            tags.append(next(it))
        elif arg == "--build-arg":
            key, _, value = next(it).partition("=")
            build_args[key] = value
        elif arg == "--build-context" or arg.startswith("--build-context="):
            spec = next(it) if arg == "--build-context" else arg.split("=", 1)[1]
            name, _, source = spec.partition("=")
            contexts[name] = source
        elif arg in ("--target", "--platform"):
            next(it)
        elif arg == "--metadata-file":
            metadata = next(it)
        elif arg == "--push":
            push = True
        elif arg.startswith("-"):
            continue
        else:
            positional.append(arg)
    context = positional[-1] if positional else "."
    path = Path(dockerfile or os.path.join(context, "Dockerfile"))
    if not path.is_file():
        return _fail(f"ERROR: failed to read dockerfile: open {path}: no such file")
    text = path.read_text()
    if "# FAKE: fail" in text:
        return _fail("ERROR: process did not complete successfully: exit code: 1")
    for ref in _resolve_from(text, build_args):
        if not ref.endswith(":verify"):
            continue
        # A --push build runs in the docker-container builder, which
        # cannot see the daemon's tags: a same-repository base resolves
        # only through a named build context, as real BuildKit does.
        if push:
            source = contexts.get(ref, "")
            if not source.startswith("docker-image://") or "@sha256:" not in source:
                return _fail(f"ERROR: pull access denied for {repository(ref)}")
        elif _find(state, ref) is None:
            return _fail(f"ERROR: pull access denied for {repository(ref)}")
    if push:
        digest = "sha256:" + _hash("manifest", *tags)
        for tag in tags:
            state["pushed"][tag] = digest
        if metadata:
            Path(metadata).write_text(json.dumps({"containerimage.digest": digest}))
        return 0
    digests = []
    if state["store"] == "containerd":
        digests = [f"{repository(t)}@sha256:{_hash('local', t)}" for t in tags]
    _new_image(state, tags, digests, "build")
    return 0


def _inspect(state: State, args: list[str]) -> int:
    if state.get("fail_inspect"):
        return _fail("failed to connect to the docker API: connection refused")
    fmt = ""
    if args[:1] == ["--format"]:
        fmt, args = args[1], args[2:]
    image_id = _find(state, args[0]) if args else None
    if image_id is None:
        return _fail(f"Error: No such image: {args[0] if args else ''}")
    image = state["images"][image_id]
    if fmt == "{{len .RepoDigests}}":
        print(len(image["digests"]))
    elif fmt == "{{range .RepoDigests}}{{println .}}{{end}}":
        sys.stdout.write("".join(f"{d}\n" for d in image["digests"]))
        print()
    elif fmt:
        return _fail(f"fakedocker: unsupported inspect format {fmt!r}", 2)
    else:
        print(
            json.dumps(
                [
                    {
                        "Id": image_id,
                        "RepoTags": image["tags"],
                        "RepoDigests": image["digests"],
                    }
                ]
            )
        )
    return 0


def _image_ls(state: State, args: list[str]) -> int:
    if state.get("fail_ls"):
        return _fail("Cannot connect to the Docker daemon")
    if args != ["--format", "{{.Repository}}:{{.Tag}}"]:
        return _fail(f"fakedocker: unsupported image ls {args!r}", 2)
    for image in state["images"].values():
        tags = [t for t in image["tags"] if "@" not in t]
        if not tags:
            print("<none>:<none>")
        for tag in tags:
            print(tag if tag.rfind(":") > tag.rfind("/") else f"{tag}:latest")
    return 0


def _pull(state: State, args: list[str]) -> int:
    if state.get("fail_pull"):
        return _fail("Error response from daemon: manifest unknown")
    ref = [a for a in args if not a.startswith("--")][-1]
    if "--platform" in args:
        ref = args[-1]
    if "@" in ref:
        _new_image(state, [ref], [ref], "pull")
    else:
        _new_image(
            state, [ref], [f"{repository(ref)}@sha256:{_hash('pull', ref)}"], "pull"
        )
    return 0


def main(argv: list[str]) -> int:
    with open(os.environ["FAKEDOCKER_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    state = _load()
    try:
        return _dispatch(state, argv)
    finally:
        _save(state)


def _dispatch(state: State, argv: list[str]) -> int:
    if argv[:2] == ["buildx", "build"]:
        return _buildx(state, argv[2:])
    if argv[:1] == ["build"]:
        return _buildx(state, argv[1:])
    if argv[:2] == ["image", "inspect"]:
        return _inspect(state, argv[2:])
    if argv[:2] == ["image", "ls"]:
        return _image_ls(state, argv[2:])
    if argv[:2] == ["image", "rm"]:
        if argv[2] in state["refuse_rm"]:
            return _fail(f"Error: conflict: unable to remove {argv[2]}")
        if _find(state, argv[2]) is None:
            return _fail(f"Error: No such image: {argv[2]}")
        _untag(state, argv[2])
        return 0
    if argv[:1] == ["tag"]:
        if argv[2] in state.get("refuse_tag", []):
            return _fail(f"Error: refusing to tag {argv[2]}")
        source = _find(state, argv[1])
        if source is None:
            return _fail(f"Error: No such image: {argv[1]}")
        _untag(state, argv[2])
        state["images"][source]["tags"].append(familiar(argv[2]))
        return 0
    if argv[:1] == ["push"]:
        image_id = _find(state, argv[1])
        if image_id is None:
            return _fail(f"An image does not exist locally with the tag: {argv[1]}")
        digest = "sha256:" + _hash("push", argv[1], image_id)
        state["images"][image_id]["digests"].append(
            familiar(f"{repository(argv[1])}@{digest}")
        )
        state["pushed"][argv[1]] = digest
        return 0
    if argv[:1] == ["pull"]:
        return _pull(state, argv[1:])
    if argv[:1] == ["save"]:
        # Recorded, not written: the legacy merge body saves to a fixed
        # /tmp path, and tests must not litter the host.
        if _find(state, argv[1]) is None:
            return _fail(f"Error: No such image: {argv[1]}")
        return 0
    return _fail(f"fakedocker: unsupported command {argv!r}", 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
