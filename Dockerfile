# The API (and, if you want, the page) as one container. Used by Render: see render.yaml and docs/DEPLOYMENT.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# libatomic1: the node that ships with Playwright needs it before it can install anything
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libatomic1 && rm -rf /var/lib/apt/lists/*

# Python packages, then the headless Chromium that geocode uses (with the system libraries it needs)
COPY requirements.txt .
RUN pip install -r requirements.txt && playwright install --with-deps chromium

# The Google Maps scraper (the same release the app was tested with, Linux build)
ARG GMAPS_VERSION=1.18.1
RUN mkdir -p bin \
 && curl -fsSL -o bin/gmaps "https://github.com/gosom/google-maps-scraper/releases/download/v${GMAPS_VERSION}/google_maps_scraper-${GMAPS_VERSION}-linux-amd64" \
 && chmod +x bin/gmaps

COPY app app
COPY prompts prompts
COPY static static

# Hosted settings. Chats and the agent's memory live in /data: mount a disk there, or they are lost when the container restarts.
ENV HOST=0.0.0.0 SCOUT_DATA_DIR=/data GMAPS_BIN=/app/bin/gmaps SCOUT_MAPS_PARALLEL=1 CHAT_SCOPE=per_browser
RUN mkdir -p /data
EXPOSE 10000
CMD ["python", "-m", "app.server"]
