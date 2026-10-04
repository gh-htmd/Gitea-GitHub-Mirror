FROM python:3.12-alpine

LABEL maintainer="yuanweize"
LABEL description="Bulk mirror all GitHub repositories to a self-hosted Gitea instance"
LABEL org.opencontainers.image.source="https://github.com/yuanweize/gitea-github-mirror"

WORKDIR /app

# git is required by the encrypted mirroring (clone/bundle/push)
RUN apk add --no-cache git && adduser -D -u 1000 mirror

# Copy application
COPY mirror.py encrypted_mirror.py crypto.py webui.py requirements-webui.txt .env.example ./
COPY templates/ ./templates/

# Dependencies for the web UI + encrypted mirroring
# (classic mirror.py remains dependency-free when run directly)
RUN pip install --no-cache-dir -r requirements-webui.txt

# Create persistent directories
RUN mkdir -p /app/logs /app/reports /app/data && \
    chown -R mirror:mirror /app

USER mirror

# Volumes for persistent data (./data holds the UI config .env, repo keys and stats DB)
VOLUME ["/app/logs", "/app/reports", "/app/data"]

EXPOSE 5000

# Default: serve the web UI. Override the command to run the CLIs instead, e.g.:
#   docker run ... <image> python3 mirror.py --yes
#   docker run ... <image> python3 encrypted_mirror.py push --yes
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5000", "webui:app"]
