# Alpine base so the Harbor opencode adapter uses the image's packaged
# Node/npm instead of installing nvm (glibc images always pull nvm first).
FROM node:22-alpine

RUN apk add --no-cache bash curl coreutils python3 py3-pytest

WORKDIR /app

# Input data lives in the image so every trial starts from the same state.
RUN mkdir -p /app/data \
    && printf '12\n-7\n3\n12\n0\n45\n-2\n3\n9\n100\n-1\n7\n7\n23\n' > /app/data/numbers.txt
