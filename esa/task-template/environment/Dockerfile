FROM node:22-alpine
RUN apk add --no-cache bash curl coreutils python3 py3-pytest
COPY esa/initial_state.json /opt/esa/initial_state.json
WORKDIR /workspace
