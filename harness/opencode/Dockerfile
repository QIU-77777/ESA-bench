# Harness image: opencode CLI, built once and mounted read-only into every
# trial container. Node/npm come from the same base as the task image so the
# mounted /usr/local stays compatible with it.
#
# Version has no default here on purpose: scripts/build-harness.sh passes
# OPENCODE_CLI_VERSION from harness/opencode/manifest.yaml, keeping that file
# the single source.
FROM node:22-alpine AS build

ARG OPENCODE_CLI_VERSION

RUN npm i -g @opencode/cli@${OPENCODE_CLI_VERSION} && opencode --version

# The trial container mounts this image's root filesystem at /usr/local, so the
# image root must hold exactly what should appear under /usr/local: bin/, lib/,
# share/. Nothing else from the alpine base is needed.
FROM scratch

ARG OPENCODE_CLI_VERSION
LABEL opencode.version="${OPENCODE_CLI_VERSION}"

COPY --from=build /usr/local/ /
