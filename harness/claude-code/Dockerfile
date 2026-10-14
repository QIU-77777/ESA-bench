# Harness image: Claude Code CLI, built once and mounted read-only into every
# trial container. Same layout contract as harness/opencode: the image root is
# exactly the contents of /usr/local.
#
# Plain `npm install -g` mirrors Harbor's own claude-code install path on
# apk-based images (no --ignore-scripts here).
FROM node:22-alpine AS build

ARG CLAUDE_CODE_VERSION

RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
  && claude --version

FROM scratch

ARG CLAUDE_CODE_VERSION
LABEL claude-code.version="${CLAUDE_CODE_VERSION}"

COPY --from=build /usr/local/ /
