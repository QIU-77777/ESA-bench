# Harness image: pi CLI, built once and mounted read-only into every trial
# container. Same layout contract as harness/opencode: the image root is
# exactly the contents of /usr/local.
#
# --ignore-scripts mirrors Harbor's own pi install (the platform binary ships
# via optional dependencies, no postinstall needed).
FROM node:22-alpine AS build

ARG PI_VERSION

RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@${PI_VERSION} \
  && pi --version

FROM scratch

ARG PI_VERSION
LABEL pi.version="${PI_VERSION}"

COPY --from=build /usr/local/ /
