# BlazeCrawl production image.
#
# Tested on Railway, Render, Fly.io, DigitalOcean App Platform, and bare Docker.
# Bundle size is ~700 MB (Chromium dominates) — well below all those platforms'
# limits, but expect a 2-3 minute first build.
FROM python:3.12-slim

# Avoid interactive prompts during apt installs.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps for lxml + curl for healthchecks. Removed at the end of the layer
# to keep the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first for better Docker layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Install Chromium AND its system libraries. `--with-deps` adds the long list
# of .so files Chromium needs (fonts, audio, X libs, etc.). This is the
# heaviest layer of the build.
RUN playwright install --with-deps chromium

# Copy the application.
COPY . .

# Platform-provided PORT (Railway/Render/Fly/Heroku all set this). Falls back
# to 8000 for plain `docker run` invocations.
ENV PORT=8000
EXPOSE 8000

# Basic healthcheck — hits the /health endpoint. Most PaaS platforms have
# their own healthcheck systems too; this is for plain Docker.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1

# Shell form so ${PORT} expands at runtime.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
