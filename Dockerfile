FROM ubuntu:24.04

WORKDIR /usr/src/app

# System deps + Node.js (for Claude Code CLI) + GitHub CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev gcc libpq-dev curl \
    nodejs npm \
    ffmpeg git tmux \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

# Claude Code CLI + Codex CLI
RUN npm install -g @anthropic-ai/claude-code @openai/codex

# Non-root user — UID 1000 matches typical host user for bind-mount perms
# Ubuntu 24.04 ships with uid 1000 as 'ubuntu', so rename it
RUN usermod -l hivemind -d /home/hivemind -m ubuntu \
    && groupmod -n hivemind ubuntu \
    && mkdir -p /home/hivemind/.claude /home/hivemind/.cache \
    && chown -R hivemind:hivemind /home/hivemind

# Operator pattern: the host root is bind-mounted at /host (see
# docker-compose.yml), and every hardcoded path in Mordecai's repo/config/
# hooks/.env is his real host path, /home/daniel/Storage/mordecai/... — so
# /home/daniel has to resolve through the mount. The target doesn't exist at
# build time, only at runtime once /host is mounted; a symlink doesn't care.
RUN ln -sfn /host/home/daniel /home/daniel

# Python venv + deps — installed to /opt/venv so bind mounts don't clobber it
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip "setuptools<81" wheel
COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Playwright browsers (installed as root before USER switch, shared path)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN /opt/venv/bin/playwright install --with-deps chromium

# App code (overridden by bind mount in dev, baked in for production)
COPY . .

RUN mkdir -p /usr/src/app/data \
    && chown -R hivemind:hivemind /usr/src/app

USER hivemind

# The mind server binds MIND_SERVER_PORT from .env; 8421 is the default.
EXPOSE 8421
CMD ["/opt/venv/bin/python3", "launch_mind_server_and_bots.py"]
