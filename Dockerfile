# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

WORKDIR /app

# Node + the Playwright npm package are needed as a protocol client only —
# the browser itself runs in the leased test slot's `slot-playwright` pod, so
# no Chromium install (`npx playwright install --with-deps`) on this image.
# This version is lockstep-coupled to glimmung's slot-playwright run-server and
# runner capture client: Playwright rejects WebSocket clients on a different
# major/minor, so bump all three together and co-release the slot image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.npm \
    npm install --omit=dev playwright@1.60.0

COPY pyproject.toml .
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

ENV NODE_PATH=/app/node_modules
ENV PLAYWRIGHT_PACKAGE_PATH=/app/node_modules/playwright/index.js

ENTRYPOINT ["mcp-glimmung-http"]
