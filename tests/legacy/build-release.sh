#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Reference implementation for differential tests. DO NOT EDIT.
#
# The "Build and push images" step body from lfreleng-actions/docker-workflows
# build-test-release.yaml at a5a34a446183198ce6d07c4f4a0dbe973c47ac64, extracted by removing
# the ten-space YAML indent and trailing whitespace. The tests run it as
# the runner does, with bash -eo pipefail, against a scripted docker.
#
# The body below stays verbatim, so lint exceptions live in this header.
# SC2164: the runner's bash -e already stops on a failed cd.
# SC2153: OWNER (like every upper-case name) comes from the lane's env:
# block, which this standalone copy cannot see.
# shellcheck disable=SC2164,SC2153

# Build every image, push it to the resolved registries and
# capture the per-registry manifest digest (the subject the
# sign job signs/attests). Each image also exports as a
# docker archive so the test, SBOM and scan jobs consume
# the published bits. The local working tag is the
# lane-neutral <name>:verify alias, so same-repository FROM
# chains resolve exactly as in the verify lane (local tags
# never publish). Any failure fails the job: a release must
# never publish a partial image set.
cd "${PATH_PREFIX}"
# Require the v prefix (the LF release convention): the
# semver gate accepts both v1.2.3 and 1.2.3, and stripping
# an optional v would let two distinct git tags publish the
# same supposedly immutable registry tag
if [[ ! "${TAG}" =~ ^v ]]; then
  echo "::error::Release tags must carry the 'v' prefix" \
    "(got '${TAG}'); accepting both v1.2.3 and 1.2.3" \
    "would alias the same registry tag"
  exit 1
fi
version="${TAG#v}"
# Docker tags cannot carry SemVer build metadata ('+');
# normalise it to '_' — collision-safe because SemVer
# forbids '_' — so a valid signed tag such as
# v1.2.3+build.5 still releases
version="${version//+/_}"
if [[ ! "${version}" =~ ^[0-9A-Za-z._-]+$ ]]; then
  echo "::error::Invalid release version: ${version}"
  exit 1
fi
owner=$(tr '[:upper:]' '[:lower:]' <<< "${OWNER}")
multi='false'
if [ "${PLATFORMS}" != 'linux/amd64' ]; then
  multi='true'
fi
# The platform whose image feeds the test/SBOM/scan jobs:
# the runner's native platform when the build targets it,
# otherwise the first requested platform (Syft and Grype
# analyse archives statically, so a foreign-architecture
# archive still scans)
scan_platform='linux/amd64'
case ",${PLATFORMS}," in
  *,linux/amd64,*) ;;
  *) scan_platform="${PLATFORMS%%,*}" ;;
esac
# The verify and merge lanes prefix local tags with
# image_namespace, so a caller whose chain references
# <namespace>/<base>:verify through build_args builds there
# and would fail here. Aliasing each image under the
# namespace as well lets one images input travel between
# the lanes unchanged.
#
# Whether to alias is decided once, here, against Docker's
# reference grammar: runs of lowercase alphanumerics joined
# by a single '.' or '_', exactly '__', or one or more '-',
# in slash-separated components. Multi-component namespaces
# such as team/project qualify, because this is a local
# reference rather than a Docker Hub organisation, and that
# gate enforces its own narrower shape separately. Anything
# docker tag would reject, such as '-team' or 'team.',
# yields no alias rather than failing a release that
# publishes nowhere near Docker Hub.
namespace_component='[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*'
alias_namespace='false'
if [ -n "${IMAGE_NAMESPACE}" ] &&
  [[ "${IMAGE_NAMESPACE}" =~ \
    ^${namespace_component}(/${namespace_component})*$ ]]
then
  alias_namespace='true'
elif [ -n "${IMAGE_NAMESPACE}" ]; then
  echo "::notice::image_namespace '${IMAGE_NAMESPACE}'" \
    "is not a usable local reference prefix; skipping the" \
    "namespaced :verify aliases"
fi
declare -a local_tags=()
pushed='[]'
while IFS= read -r spec; do
  name=$(jq -r '.name' <<< "${spec}")
  ctx=$(jq -r '.context' <<< "${spec}")
  df=$(jq -r '.dockerfile //
    (.context + "/Dockerfile")' <<< "${spec}")
  target=$(jq -r '.target // empty' <<< "${spec}")
  declare -a common=(-f "${df}")
  if [ -n "${target}" ]; then
    common+=(--target "${target}")
  fi
  while IFS= read -r build_arg; do
    [ -n "${build_arg}" ] &&
      common+=(--build-arg "${build_arg}")
  done < <(jq -r '.build_args[]? // empty' <<< "${spec}")
  declare -a repos=()
  if [ "${GHCR_PUBLISH}" = 'true' ]; then
    repos+=("ghcr.io/${owner}/${name}")
  fi
  if [ "${DOCKERHUB}" = 'true' ]; then
    repos+=("docker.io/${IMAGE_NAMESPACE}/${name}")
  fi
  declare -a refs=()
  for repo in "${repos[@]}"; do
    refs+=("${repo}:${version}")
  done
  declare -a tag_args=()
  for ref in "${refs[@]}"; do
    tag_args+=(-t "${ref}")
  done
  local_tag="${name}:verify"
  # The pushed digest captures from the push operation
  # itself (buildx metadata / the local daemon's post-push
  # record), never re-read through the mutable version tag:
  # a concurrent writer moving the tag cannot desynchronise
  # what the sign job signs from what this run pushed
  image_digest=''
  if [ "${multi}" = 'true' ]; then
    if [ "${#repos[@]}" -gt 0 ]; then
      # Manifest-list build pushed straight to the
      # registries; buildx SBOM/provenance stay off — the
      # sign job attests provenance by digest instead
      echo "::group::Build/push ${name} (${PLATFORMS})"
      docker buildx build --platform "${PLATFORMS}" \
        --push --provenance=false --sbom=false \
        --metadata-file /tmp/build-meta.json \
        "${tag_args[@]}" "${common[@]}" "${ctx}"
      echo "::endgroup::"
      # One build pushes the same manifest list to every
      # repo, so the emitted digest covers them all
      image_digest=$(jq -r \
        '."containerimage.digest" // empty' \
        /tmp/build-meta.json)
      # The archive the test/SBOM/scan jobs consume pulls
      # back from the pushed manifest by its immutable
      # digest, so downstream jobs audit the published bits
      echo "::group::Pull ${name} (${scan_platform})"
      docker pull --platform "${scan_platform}" \
        "${repos[0]}@${image_digest}"
      docker tag "${repos[0]}@${image_digest}" \
        "${local_tag}"
      echo "::endgroup::"
    else
      # Nothing publishes, so there is no registry to hold
      # a manifest list. Build the scan platform alone,
      # naming it explicitly rather than letting buildx
      # default to the runner's: a silent native-only
      # build lets a dry run of a linux/arm64 release pass
      # without arm64 ever being built, which is the part
      # most likely to break. The remaining platforms are
      # not built, and the warning says so.
      if [ "${PLATFORMS}" != "${scan_platform}" ]; then
        echo "::warning::No registry resolved, so" \
          "${name} builds only ${scan_platform}; the" \
          "other platforms in '${PLATFORMS}' are not" \
          "built and no manifest list is produced"
      fi
      echo "::group::Build ${local_tag} (${scan_platform})"
      docker buildx build --platform "${scan_platform}" \
        --load -t "${local_tag}" "${common[@]}" "${ctx}"
      echo "::endgroup::"
    fi
  else
    echo "::group::Build ${local_tag}"
    docker buildx build --load -t "${local_tag}" \
      "${tag_args[@]}" "${common[@]}" "${ctx}"
    echo "::endgroup::"
    for ref in "${refs[@]}"; do
      echo "::group::Push ${ref}"
      docker push "${ref}"
      echo "::endgroup::"
    done
  fi
  for repo in "${repos[@]}"; do
    digest="${image_digest}"
    if [ -z "${digest}" ]; then
      # Single-platform path: the daemon records
      # repo@digest locally when it pushes, so this reads
      # the digest of our own push, not the registry's
      # current tag state
      digest=$(docker image inspect \
        --format \
        '{{range .RepoDigests}}{{println .}}{{end}}' \
        "${local_tag}" \
        | awk -F'@' -v repo="${repo}" \
          '$1 == repo {print $2; exit}')
    fi
    if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "::error::Failed to resolve digest for" \
        "${repo}:${version} (got: ${digest})"
      exit 1
    fi
    echo "${repo}@${digest}"
    pushed=$(jq -c \
      --arg n "${name}" --arg i "${repo}" \
      --arg d "${digest}" \
      '. + [{name: $n, image: $i, digest: $d}]' \
      <<< "${pushed}")
  done
  local_tags+=("${local_tag}")
  if [ "${alias_namespace}" = 'true' ]; then
    alias_repo="${IMAGE_NAMESPACE}/${name}"
    # Docker caps a repository name at 255 characters, so
    # a long namespace can carry a grammatical alias past
    # the limit even though every component is well
    # formed. Checking the assembled name keeps the
    # promise above: an alias this daemon would refuse is
    # skipped, never a reason for a release to fail.
    if [ "${#alias_repo}" -le 255 ]; then
      docker tag "${local_tag}" "${alias_repo}:verify"
    else
      echo "::notice::Skipping the namespaced alias for" \
        "${name}: the assembled repository name is" \
        "${#alias_repo} characters, over Docker's limit" \
        "of 255"
    fi
  fi
done < <(jq -c '.[]' <<< "${IMAGES_JSON}")
built=$(printf '%s\n' "${local_tags[@]}" | jq -cnR '[inputs]')
pushed_count=$(jq 'length' <<< "${pushed}")
echo "Built ${#local_tags[@]} image(s): ${built}"
echo "Pushed ${pushed_count} image reference(s)"
{
  echo "images=${built}"
  echo "image_count=${#local_tags[@]}"
  # docker-save-images-action takes a space-separated list;
  # an image reference cannot contain whitespace, so the
  # join stays unambiguous
  echo "images_list=${local_tags[*]}"
  echo "pushed=${pushed}"
  echo "pushed_count=${pushed_count}"
} >> "$GITHUB_OUTPUT"
{
  echo "## Docker Build/Push"
  echo ""
  echo "Built **${#local_tags[@]}** image(s);" \
    "pushed **${pushed_count}** reference(s):"
  echo ""
  if [ "${pushed_count}" -gt 0 ]; then
    jq -r '.[] | "- `\(.image)@\(.digest)`"' \
      <<< "${pushed}"
  else
    echo "- (nothing pushed)"
  fi
  echo ""
} >> "$GITHUB_STEP_SUMMARY"
