<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🐳 Docker Build Images Action

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/docker-build-images-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![pre-commit.ci status badge]][pre-commit.ci results page] [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/docker-build-images-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/docker-build-images-action)
<!-- prettier-ignore-end -->

Builds a list of container images in order, then either loads them
into the local Docker daemon or pushes them to registries, capturing
each pushed digest. It reports every image as built, failed or skipped.

The image list comes from [docker-build-matrix-action], which finds
the images and orders them so that same-repository base images build
first. This action builds them.

## docker-build-images-action

Two modes cover the [docker-workflows] lanes:

- **`mode: load`** builds each image into the daemon as
  `<name>:verify`, for later jobs to test, scan and publish. The verify
  and merge lanes use it.
- **`mode: push`** builds and pushes each image to every repository and
  tag, recording the digest of each push for signing. The release lane
  uses it.

Every default reproduces the build loops those lanes carried inline
([docker-workflows#34]), so each lane can swap its build step for this
action. The test suite proves that against the original step bodies;
see [Compatibility](#compatibility).

## Usage Example

### Verify: build, then test and scan the images

<!-- markdownlint-disable MD046 -->

```yaml
steps:
  - uses: actions/checkout@<sha>  # vX.Y.Z
  - id: images
    uses: lfreleng-actions/docker-build-matrix-action@<sha>  # vX.Y.Z
    with:
      order: dependencies
  - id: build
    uses: lfreleng-actions/docker-build-images-action@<sha>  # vX.Y.Z
    with:
      images: ${{ steps.images.outputs.images_json }}
  - uses: lfreleng-actions/docker-save-images-action@<sha>  # vX.Y.Z
    with:
      docker-artifacts-to-save: ${{ steps.build.outputs.images_list }}
      mode: per-image
```

### Release: push, then sign each pushed digest

```yaml
- id: build
  uses: lfreleng-actions/docker-build-images-action@<sha>  # vX.Y.Z
  with:
    mode: push
    images: ${{ steps.images.outputs.images_json }}
    repositories: |
      ghcr.io/my-org
      docker.io/my-org
    tags: '1.2.3'
    platforms: 'linux/amd64,linux/arm64'
# steps.build.outputs.pushed: [{"name":..., "image":..., "digest":...}]
```

### Project tooling builds the images

The `build_command` escape hatch is for repositories whose own tooling
builds images, such as Maven's fabric8 plugin, jib, Gradle or a
Makefile:

```yaml
- uses: lfreleng-actions/docker-build-images-action@<sha>  # vX.Y.Z
  with:
    build_command: 'mvn -B -P docker clean install'
    build_command_images: 'onap/cps:latest onap/ncmp:latest'
```

`build_command_images` names the images the command produces. Leave
it empty to have the action infer them from the image list around the
command. On Docker's classic image store a local build carries no
registry digest while a pulled base does, so an image that appeared
without one came from this build. Creation times are never read, so a
builder stamping a reproducible one, such as jib, infers like any
other. On the containerd image store every image carries a digest; with
no digest-free image to prove which is which, local builds and pulled
bases look alike. The action then reports the images by name and asks
for `build_command_images`, rather than guessing.

### Extra buildx flags

`build_flags` passes arguments to every build, as `docker-build-args`
does in global-jjb. An images entry's own `build_flags` list adds flags
for that image alone:

```yaml
with:
  build_flags: '--network=host --pull'
```

<!-- markdownlint-enable MD046 -->

The action refuses the flags it sets itself, so none can contradict
the mode: the tags, `--load`, `--push`, `--output`, `--platform`,
`--file`, `--metadata-file` and `--builder`. It also refuses `--call`,
`--check` and `--help`, which can succeed without producing an image
that would then read as built.

## Inputs

<!-- markdownlint-disable MD013 -->

| Name                   | Required | Default   | Description                                                                                        |
| ---------------------- | -------- | --------- | -------------------------------------------------------------------------------------------------- |
| `images`               | False    | `''`      | Images in build order: docker-build-matrix-action's `images_json` or `matrix` output               |
| `path_prefix`          | False    | `.`       | Project root; contexts and Dockerfiles are relative to it                                          |
| `image_namespace`      | False    | `''`      | Namespace for local tags; see [Local tags](#local-tags)                                            |
| `local_tag`            | False    | `verify`  | Tag for each image's local working copy                                                            |
| `build_flags`          | False    | `''`      | Extra buildx arguments for every image, split as a shell splits them                               |
| `mode`                 | False    | `load`    | `load` into the daemon, or `push` to registries                                                    |
| `repositories`         | False    | `''`      | `mode: push`: repository prefixes; each image pushes as `<prefix>/<name>`. Empty is a dry run      |
| `tags`                 | False    | `''`      | `mode: push`: tags to push; required with `repositories`                                           |
| `platforms`            | False    | `''`      | `mode: push`: comma-separated platforms; empty is the runner's                                     |
| `build_permit_fail`    | False    | `false`   | `mode: load`: report failed builds and pass the step                                               |
| `skip_dependents`      | False    | `false`   | `mode: load`: skip an image whose same-repository base did not build                               |
| `build_command`        | False    | `''`      | `mode: load`: the project's own image build command                                                |
| `build_command_images` | False    | `''`      | References `build_command` produces; skips inference                                               |
| `setup_buildx`         | False    | `true`    | Set up buildx with the driver the build needs                                                      |
| `summary`              | False    | `true`    | Write a build report to the step summary                                                           |

<!-- markdownlint-enable MD013 -->

## Outputs

<!-- markdownlint-disable MD013 -->

| Name           | Description                                                                          |
| -------------- | ------------------------------------------------------------------------------------ |
| `images`       | JSON list of the local tags built, in build order                                    |
| `image_count`  | Number of images built                                                               |
| `images_list`  | Space-separated local tags, the form docker-save-images-action takes                 |
| `failed`       | JSON list of failed builds: tags, or `build_command` when the escape hatch fails     |
| `failed_count` | Number of failed builds                                                              |
| `results`      | JSON list of `{name, tag, status}`, status `built`, `failed` or `skipped`            |
| `pushed`       | `mode: push`: JSON list of `{name, image, digest}`, one per pushed repository        |
| `pushed_count` | Number of pushed image repositories                                                  |

<!-- markdownlint-enable MD013 -->

## Implementation Details

### Build order and failures

Images build in the order given. A base must come before the images
built from it, which docker-build-matrix-action's `order: dependencies`
guarantees.

In `mode: load` every image gets an attempt, because a later image can
build when an earlier one does not, and a full picture of what breaks
helps more than stopping at the first failure. An image whose base
failed then fails in turn. With `skip_dependents`, and entries carrying
`depends_on` (from docker-build-matrix-action's `matrix` output), the
action skips it instead and reports that.

The step fails when any build fails, unless `build_permit_fail` allows
it. The action writes outputs and the summary first, so whatever did
build stays visible either way.

`mode: push` stops at the first failure, and refuses `build_permit_fail`
before anything builds: a partial publication is never a success. The
failed image records as `failed`, and the
images after it as `skipped`, without building. Registries offer no
transaction to roll back, so images pushed before the failure stay
pushed. `pushed` lists every repository whose push completed, even when
the same image failed afterwards, say pulling its scan copy back; the
failed image may also have pushed some tags that `pushed` cannot list.
A release caller should treat a failed run as a partial publication,
and fix and re-run it rather than assume nothing reached a registry.

### Local tags

In `mode: load` each image builds as `<namespace>/<name>:<local_tag>`
and, with a namespace, also carries the tag `<name>:<local_tag>`. So a
same-repository `FROM` resolves in either spelling.

In `mode: push` the local copy is `<name>:<local_tag>`. The action
adds the namespaced alias when the namespace is a usable local prefix,
and skips it with a notice otherwise, since local tags never publish.

### Pushing and digests

A single-platform build loads locally, then pushes each reference. The
digest comes from the daemon's record of that push, never re-read
through the tag, so a concurrent writer moving the tag cannot make a
signing job sign something other than what this run pushed.

A multi-platform build pushes a manifest list straight to the
registries, and takes its digest from buildx's build metadata. It then
pulls one platform back by that digest, so later jobs test and scan
the published bits. That scan platform is the runner's own when the
build includes it, and otherwise the first platform requested: a
release for `linux/arm64` alone on an amd64 runner loads its arm64 image,
which Syft and Grype still analyse, since they read archives
statically.

That build runs in a `docker-container` builder, which cannot see the
daemon's local tags, so a later image's `FROM base:verify` would try to
pull a public `base`. So each pushed image joins every later
build as a named build context, `--build-context
base:verify=docker-image://<repository>@<digest>`, and BuildKit
resolves the `FROM` to the pushed manifest list, platform by platform.
Same-repository chains work multi-platform as they do single-platform.

With no repositories there is nowhere to
hold a manifest list, so the scan platform alone builds, with a
warning.

### Builder

The composite action plans the builder before building. The `docker`
driver builds against the daemon's own image store, which
same-repository chains need: under `docker-container`, an image one
build loads is invisible to the next build's `FROM`. A multi-platform
push is the one case that needs `docker-container`, which also brings
in QEMU. Every build, `build_command`'s included, then runs on that
builder through `BUILDX_BUILDER`, because setting up the `docker`
driver selects nothing and a `docker-container` builder the job
selected earlier would otherwise take the builds. Set
`setup_buildx: false` to use a builder the job has already set up; the
action then leaves the builder choice to the job.

## Compatibility

`tests/legacy/` holds the three lanes' build step bodies, extracted
verbatim from docker-workflows at
`a5a34a446183198ce6d07c4f4a0dbe973c47ac64`. `tests/test_equivalence.py`
runs each of them and the action against the same scripted `docker`,
and compares:

- exit status;
- annotations;
- outputs;
- every docker call that changes state, in order.

The scenarios are the loop cases for the verify lane, both permissive
and strict, and the merge lane; the escape-hatch decision table from
docker-workflows PR #65, on both image stores; that PR's
declared-reference corpus; and push mode against the release lane.

Four differences are deliberate:

- The merge lane saves image archives itself. The action leaves that to
  docker-save-images-action, as the verify and release lanes already
  do. The merge lane's publish job loads every archive it finds, so
  archive names do not matter.
- When a release build fails, the release lane's `set -e` ends the run
  with no annotation, no outputs, and its log group still open. The
  action reports which build failed, closes the group, and still
  publishes its outputs.
- In a multi-platform push, later builds gain `--build-context`
  arguments naming earlier pushed images by digest, so same-repository
  bases resolve inside the `docker-container` builder. The release lane
  cannot build such a chain at all.
- A single-platform push to Docker Hub resolves its digest. Docker
  reports `RepoDigests` in familiar form (`onap/x`, not
  `docker.io/onap/x`), so the release lane's exact match finds nothing
  and fails a push that succeeded. The action compares familiar forms.

Outside those scenarios the action is stricter where the lanes fail
part-way. It refuses before anything builds a malformed `images` entry,
an invalid image name or `image_namespace` component, or a repository
path over Docker's 255-character limit, counted as Docker counts it (a
single-component `docker.io/x` is `library/x`, which the lanes miss);
the lanes discover each of these mid-run. A failed
load-mode alias fails that one image, where the lanes' `set -e` ends
the run with no outputs.

Inputs take the lanes' names, so each lane's `env:` block maps across
one for one. The release lane's `GHCR_PUBLISH`, `DOCKERHUB` and
`OWNER` become `repositories`, and its version becomes `tags`.

## Notes

- Needs `python3` 3.10 or later on the runner, as GitHub-hosted runners
  provide. The action installs nothing beyond buildx and QEMU.
- Building runs repository-controlled code: every `Dockerfile`'s `RUN`
  instructions execute in BuildKit, and `build_command` runs on the
  runner itself. Treat an untrusted checkout's builds as untrusted
  code. Python's isolated mode guarantees something narrower: the
  checked-out tree stays off the import path, so a repository cannot
  substitute its own modules for the action's.

### Development

```bash
python3 -m unittest discover -s tests -t .
```

`tests/fakedocker.py` stands in for the Docker CLI, so the suite runs
offline. The differential tests also need `bash` and `jq`. The CI
workflow adds real builds, a real registry, and multi-platform pushes.

[docker-build-matrix-action]: https://github.com/lfreleng-actions/docker-build-matrix-action
[docker-workflows]: https://github.com/lfreleng-actions/docker-workflows
[docker-workflows#34]: https://github.com/lfreleng-actions/docker-workflows/issues/34
[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/docker-build-images-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/docker-build-images-action/main.svg
