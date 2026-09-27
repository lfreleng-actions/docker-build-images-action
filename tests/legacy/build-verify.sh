#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Reference implementation for differential tests. DO NOT EDIT.
#
# The "Build images" step body from lfreleng-actions/docker-workflows
# build-test.yaml at a5a34a446183198ce6d07c4f4a0dbe973c47ac64, extracted by removing
# the ten-space YAML indent and trailing whitespace. The tests run it as
# the runner does, with bash -eo pipefail, against a scripted docker.
#
# The body below stays verbatim, so lint exceptions live in this header.
# SC2164: the runner's bash -e already stops on a failed cd.
# shellcheck disable=SC2164

# Build every image, then export each to a docker archive so
# the test/SBOM jobs can consume the exact bits built here.
cd "${PATH_PREFIX}"
if [ -n "${IMAGE_NAMESPACE}" ] &&
  [[ ! "${IMAGE_NAMESPACE}" =~ ^[a-z0-9._-]+(/[a-z0-9._-]+)*$ ]]
then
  echo "::error::Invalid image_namespace" \
    "'${IMAGE_NAMESPACE}' (lowercase path components" \
    "without leading/trailing/consecutive slashes)"
  exit 1
fi
# build_command_images names what build_command produces, so
# it says nothing on its own. Ignoring it would leave a
# caller believing they had declared their images while the
# workflow discovered a different set from Dockerfiles and
# carried that downstream instead — a misconfiguration that
# would otherwise succeed.
if [ -n "${BUILD_COMMAND_IMAGES}" ] &&
  [ -z "${BUILD_COMMAND}" ]
then
  echo "::error::build_command_images requires" \
    "build_command; it names the images that command" \
    "produces and has no meaning without it"
  exit 1
fi
declare -a tags=()
declare -a failures=()
declare -a unattributed=()
if [ -n "${BUILD_COMMAND}" ]; then
  # Escape hatch: the project's own tooling builds the
  # images; enumerate what it created by diffing the image
  # list around the command. Both a locally built image and
  # a base the command pulled show up as new names, so
  # something has to tell them apart; see the candidate
  # loop below for how, and why one signal is not enough.
  #
  # Declared references are validated and cleared first,
  # because both have to happen before the command runs.
  declare -a declared_images=()
  while IFS= read -r declared; do
    [ -n "${declared}" ] || continue
    # Validate the whole reference, since all of it
    # reaches docker and crane. '@' is excluded outright:
    # a digest reference has no tag to publish under, and
    # the publish name comes from stripping the tag, which
    # would leave '@sha256' behind.
    #
    # An IPv6 registry is bracketed, so it needs its own
    # shape here: the brackets are the only characters in a
    # reference this gate would otherwise refuse outright,
    # and refusing them would leave a caller addressing a
    # registry that way unable to declare anything at all.
    ipv6_host='\[[0-9A-Fa-f:]+\]'
    body='[A-Za-z0-9][A-Za-z0-9._/:-]*'
    if [[ ! "${declared}" =~ ^${body}$ ]] &&
      [[ ! "${declared}" =~ ^${ipv6_host}(:[0-9]+)?/${body}$ ]]
    then
      echo "::error::Invalid build_command_images entry:" \
        "${declared} (expected name:tag, no digest)"
      exit 1
    fi
    ref_body="${declared%:*}"
    ref_tag="${declared##*:}"
    # Cutting at the last colon finds a tag only when the
    # final component carries one. On
    # 'localhost:5000/team/app' it finds the registry port
    # instead, which is how such a value would come to
    # publish under the name 'localhost'.
    if [ "${ref_body}" = "${declared}" ] ||
      [[ "${ref_tag}" == */* ]]; then
      echo "::error::build_command_images entry" \
        "'${declared}' needs a tag on its final component"
      exit 1
    fi
    if [[ ! "${ref_tag}" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ ]]
    then
      echo "::error::build_command_images entry" \
        "'${declared}' has an invalid tag '${ref_tag}'"
      exit 1
    fi
    # 'sha256:<hex>' is how docker names an image by id, not
    # a repository and a tag, and it reads as the latter
    # here: 'sha256' passes as a name and the hex passes as a
    # tag. Left alone, 'docker image inspect' would resolve
    # it against any image already on the daemon, so the
    # post-command check could pass on something the command
    # never produced -- and the publish name, taken by
    # stripping the tag, would come out as 'sha256'. Docker
    # refuses the form outright; refuse it before the command
    # runs rather than after.
    if [ "${ref_body}" = 'sha256' ]; then
      echo "::error::build_command_images entry" \
        "'${declared}' names an image by id, not a" \
        "repository and tag"
      exit 1
    fi
    # Every path component has to hold up, not just the
    # last: docker rejects 'team//app', 'team/_bad/app' and
    # 'team:port/app' as surely as a bad name, and without
    # this the command runs before any of that surfaces.
    #
    # A trailing slash has to be caught before the split
    # rather than by the loop below: 'read' discards one
    # trailing empty field, so 'app/' splits to a single
    # 'app' and the gap disappears before anything can look
    # at it. Only one field goes, so 'app//' still leaves an
    # empty for the loop, which is why this covers the one
    # case rather than standing in for that check.
    if [[ "${ref_body}" == */ ]]; then
      echo "::error::build_command_images entry" \
        "'${declared}' has an empty path component"
      exit 1
    fi
    old_ifs="${IFS}"
    IFS='/'
    read -ra ref_parts <<< "${ref_body}"
    IFS="${old_ifs}"
    component='^[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*$'
    # Docker's domain grammar, spelled out: a registry is
    # dot-separated labels, each starting and ending
    # alphanumeric with hyphens only inside, and an optional
    # port. Matching the characters without the boundaries
    # would admit 'bad-.example', 'bad..example' and
    # 'example.', all of which docker refuses to parse.
    label='[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?'
    registry="^(${label}([.]${label})*|${ipv6_host})(:[0-9]+)?$"
    has_registry='false'
    part_index=0
    for part in "${ref_parts[@]}"; do
      if [ -z "${part}" ]; then
        echo "::error::build_command_images entry" \
          "'${declared}' has an empty path component"
        exit 1
      fi
      # A leading component carrying a dot or a colon, or
      # named localhost, is a registry rather than a path
      # component — docker's own rule for telling them
      # apart.
      #
      # Docker goes on to read such a component as a Docker
      # Hub path when it does not parse as a registry, so it
      # accepts 'a_b.c/x:v1' and 'UPPER/x:v1'. This refuses
      # them, and that asymmetry is deliberate: refusing a
      # reference docker would take costs a declaration that
      # never ran, while accepting one docker rejects spends
      # a whole build before anything surfaces. Parity with
      # the real parser belongs where it can be tested
      # directly against it, which is the extraction in #34.
      if [ "${part_index}" -eq 0 ] &&
        [ "${#ref_parts[@]}" -gt 1 ] &&
        { [[ "${part}" == *.* ]] || [[ "${part}" == *:* ]] ||
          [ "${part}" = localhost ]; }
      then
        has_registry='true'
        if [[ ! "${part}" =~ ${registry} ]]
        then
          echo "::error::build_command_images entry" \
            "'${declared}' has an invalid registry" \
            "'${part}'"
          exit 1
        fi
      elif [[ ! "${part}" =~ ${component} ]]; then
        echo "::error::build_command_images entry" \
          "'${declared}' has an invalid path component" \
          "'${part}'"
        exit 1
      fi
      part_index=$((part_index + 1))
    done
    # Docker caps the repository path -- the reference minus
    # any registry -- at 255 characters and refuses a longer
    # one however well formed its components are, so without
    # this the command runs before the reference surfaces as
    # merely absent. A single component is normalised to
    # 'library/<name>' first, which spends eight of those
    # characters before the name gets any -- but only where
    # there is no registry, since that normalisation is for
    # bare Docker Hub names. The same limit is kept on
    # assembled namespace aliases in build-test-release.yaml.
    ref_path="${ref_body}"
    if [ "${has_registry}" = 'true' ]; then
      ref_path="${ref_body#*/}"
    elif [[ "${ref_path}" != */* ]]; then
      ref_path="library/${ref_path}"
    fi
    if [ "${#ref_path}" -gt 255 ]; then
      echo "::error::build_command_images entry" \
        "'${declared}' resolves to a" \
        "${#ref_path}-character repository path, over" \
        "Docker's limit of 255"
      exit 1
    fi
    # A reference named twice would be saved twice: the
    # archive names derive from it, so the per-image save
    # collides in one lane and overwrites in the other.
    # Rejecting matches how the images input treats
    # duplicates, and a repeat says nothing a single entry
    # does not.
    for existing in "${declared_images[@]}"; do
      if [ "${existing}" = "${declared}" ]; then
        echo "::error::build_command_images lists" \
          "${declared} more than once"
        exit 1
      fi
    done
    declared_images+=("${declared}")
  done < <(tr ',' ' ' <<< "${BUILD_COMMAND_IMAGES}" \
    | tr -s '[:space:]' '\n')
  # A value made only of separators parses to nothing. Left
  # alone it would fall through to inference, so a caller who
  # supplied the input precisely to replace inference would
  # get it anyway, and on a store where the digest does not
  # discriminate they would get the unattributable-images
  # error while holding the input that answers it.
  if [ -n "${BUILD_COMMAND_IMAGES}" ] &&
    [ "${#declared_images[@]}" -eq 0 ]
  then
    echo "::error::build_command_images is set but names no" \
      "references; list the images the command produces, or" \
      "remove the input to enumerate them by inference"
    exit 1
  fi
  # Drop any existing tag of the same name, so a reference
  # present afterwards is one this command produced.
  # Otherwise a daemon reused between runs lets a command
  # that quietly built nothing hand its previous image to
  # the test, SBOM and scan jobs as though it were fresh.
  for declared in "${declared_images[@]}"; do
    if docker image inspect "${declared}" \
      >/dev/null 2>&1; then
      echo "::notice::Clearing existing ${declared} so the" \
        "command has to produce it"
      # A refusal has to stop the run. Docker declines to
      # remove a tag a container still references, and
      # carrying on would leave the stale image in place
      # for the check below to accept as fresh output,
      # which is the hole this clearing exists to close.
      if ! docker image rm "${declared}" >/dev/null 2>&1
      then
        echo "::error::Could not clear the existing" \
          "${declared}; with that tag still in place a" \
          "command that builds nothing would look" \
          "successful. Remove any container holding it"
        exit 1
      fi
    fi
  done
  docker image ls --format '{{.Repository}}:{{.Tag}}' \
    | sort -u > /tmp/images-before.txt
  echo "::group::build_command"
  command_status=0
  bash -e -c "${BUILD_COMMAND}" || command_status=$?
  echo "::endgroup::"
  if [ "${command_status}" -ne 0 ]; then
    echo "::error::build_command exited ${command_status}"
    failures+=("build_command")
  fi
  if [ "${#declared_images[@]}" -gt 0 ]; then
    # Declared: no inference at all. The caller names what
    # the command produces, which is the only account that
    # holds for a builder stamping a reproducible creation
    # timestamp, and it costs a caller who knows their own
    # image names nothing to supply. Each reference was
    # cleared above, so its presence here means this
    # invocation created it.
    for declared in "${declared_images[@]}"; do
      if docker image inspect "${declared}" \
        >/dev/null 2>&1; then
        tags+=("${declared}")
      else
        echo "::error::build_command_images names" \
          "${declared}, which the command did not create"
        failures+=("${declared}")
      fi
    done
  else
    docker image ls --format '{{.Repository}}:{{.Tag}}' \
      | sort -u > /tmp/images-after.txt
    while IFS= read -r candidate; do
      # A registry digest is the only evidence available
      # here that an image came from a registry rather
      # than from this command, and it discriminates under
      # the classic image store, where a local build
      # carries none.
      #
      # The containerd image store gives every image a
      # digest, so the evidence stops discriminating. Which
      # store this is shows in the run itself: a candidate
      # without a digest can only have been built here, and
      # proves the store gives local builds none, which
      # makes the digest-bearing candidates pulled bases.
      # Absent that proof the two readings cannot be told
      # apart, so those names are held rather than resolved,
      # and settled below where the answer is known.
      #
      # The creation timestamp is deliberately not consulted.
      # A pulled base carries its upstream build date while a
      # fresh build carries this run's, but that is metadata
      # the image holds rather than proof of where it came
      # from: a base published while the command ran would
      # read as locally built, and a builder stamping a
      # reproducible timestamp -- jib pins it to the Unix
      # epoch -- would read as pulled, on exactly the store
      # where nothing else can tell.
      digests=$(docker image inspect \
        --format '{{len .RepoDigests}}' "${candidate}")
      if [ "${digests}" = "0" ]; then
        tags+=("${candidate}")
      else
        unattributed+=("${candidate}")
      fi
    done < <(comm -13 /tmp/images-before.txt \
      /tmp/images-after.txt | grep -v '<none>' || true)
    # Something was built here without a digest, so this
    # store gives local builds none and the held names are
    # pulled bases after all.
    if [ "${#tags[@]}" -gt 0 ]; then
      for held in "${unattributed[@]}"; do
        echo "::notice::Skipping pulled image ${held}" \
          "(registry digest, on a store that gives local" \
          "builds none)"
      done
      unattributed=()
    fi
  fi
  # Nothing to carry downstream. Which of the two things
  # went wrong depends on what the daemon left behind, and
  # the attribution message is reported whether or not the
  # command also exited non-zero: it names the images and the
  # input that settles them, which is the actionable part,
  # and a failing command is exactly when a caller most needs
  # it. Under build_permit_fail the job carries on, so
  # without this the run would end green having silently
  # carried nothing.
  #
  # The bare "created nothing" line stays gated on there
  # being no prior failure, since a non-zero exit has already
  # said that: report it once, not twice. Likewise the
  # failure is recorded only when it is not already there.
  if [ "${#tags[@]}" -eq 0 ]; then
    if [ "${#unattributed[@]}" -gt 0 ]; then
      echo "::error::build_command left" \
        "${#unattributed[@]} image(s) this run cannot" \
        "attribute: ${unattributed[*]}. Every new image" \
        "here carries a registry digest, which means a" \
        "pulled base on one image store and proves nothing" \
        "on another, so either the command built these and" \
        "they need naming with the build_command_images" \
        "input, or it built nothing at all"
    elif [ "${#failures[@]}" -eq 0 ]; then
      echo "::error::build_command completed but created" \
        "no new images. Name them with the" \
        "build_command_images input if the command builds" \
        "images this workflow cannot observe"
    fi
    if [ "${#failures[@]}" -eq 0 ]; then
      failures+=("build_command")
    fi
  fi
else
  while IFS= read -r spec; do
    name=$(jq -r '.name' <<< "${spec}")
    ctx=$(jq -r '.context' <<< "${spec}")
    df=$(jq -r '.dockerfile //
      (.context + "/Dockerfile")' <<< "${spec}")
    target=$(jq -r '.target // empty' <<< "${spec}")
    tag="${name}:verify"
    if [ -n "${IMAGE_NAMESPACE}" ]; then
      tag="${IMAGE_NAMESPACE}/${tag}"
    fi
    args=(--load -f "${df}" -t "${tag}")
    if [ -n "${target}" ]; then
      args+=(--target "${target}")
    fi
    while IFS= read -r build_arg; do
      [ -n "${build_arg}" ] &&
        args+=(--build-arg "${build_arg}")
    done < <(jq -r '.build_args[]? // empty' <<< "${spec}")
    echo "::group::Build ${tag}"
    build_status=0
    docker buildx build "${args[@]}" "${ctx}" ||
      build_status=$?
    echo "::endgroup::"
    # Every image gets an attempt: a later image can build
    # when an earlier one does not, and a full picture of
    # what breaks beats stopping at the first failure. An
    # image whose same-repository base failed fails in
    # turn, which is the accurate signal.
    if [ "${build_status}" -eq 0 ]; then
      tags+=("${tag}")
      # With a namespace the alias becomes
      # <namespace>/<name>:verify; also apply the plain
      # <name>:verify alias so auto-discovered chains
      # (FROM <name>:verify) resolve either way, as they
      # already do in the merge lane
      if [ -n "${IMAGE_NAMESPACE}" ]; then
        docker tag "${tag}" "${name}:verify"
      fi
    else
      echo "::error::Build failed for ${tag} (exit" \
        "${build_status})"
      failures+=("${tag}")
    fi
  done < <(jq -c '.[]' <<< "${IMAGES_JSON}")
fi
if [ "${#tags[@]}" -gt 0 ]; then
  built=$(printf '%s\n' "${tags[@]}" | jq -cnR '[inputs]')
else
  built='[]'
fi
echo "Built ${#tags[@]} image(s): ${built}"
{
  echo "images=${built}"
  echo "image_count=${#tags[@]}"
  # docker-save-images-action takes a space-separated list.
  # An image reference cannot contain whitespace, so the
  # join stays unambiguous, and an empty array yields an
  # empty string, which the action reads as nothing to save
  echo "images_list=${tags[*]}"
} >> "$GITHUB_OUTPUT"
{
  echo "## Docker Build"
  echo ""
  echo "Built **${#tags[@]}** image(s):"
  echo ""
  if [ "${#tags[@]}" -gt 0 ]; then
    printf -- '- %s\n' "${tags[@]}"
  else
    echo "- (none)"
  fi
  echo ""
  if [ "${#failures[@]}" -gt 0 ]; then
    # Entries are image tags, or the literal 'build_command'
    # when the escape hatch fails, so the wording stays
    # neutral rather than claiming every entry is an image.
    echo "Failed **${#failures[@]}** build(s):"
    echo ""
    printf -- '- %s\n' "${failures[@]}"
    echo ""
  fi
} >> "$GITHUB_STEP_SUMMARY"
# Outputs and the summary land before the gate, so whatever
# did build stays visible either way.
if [ "${#failures[@]}" -gt 0 ]; then
  if [ "${PERMIT_FAIL}" != "true" ]; then
    exit 1
  fi
  echo "::warning::${#failures[@]} build(s) failed;" \
    "permitted by build_permit_fail"
  echo "⚠️ Build failures permitted by build_permit_fail" \
    >> "$GITHUB_STEP_SUMMARY"
fi
