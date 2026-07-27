#!/usr/bin/env bash
# hive-edge-minds setup — scaffold a mind and emit its installer.
#
# Interactive by default; every answer can be provided as a flag for
# unattended runs:
#
#   ./setup.sh --name atlas --profile standard --role operator --deployment systemd \
#              --harness claude_cli_claude --surfaces telegram --port 8421
#
# What it does:
#   1. Scaffolds minds/<name>/ (runtime.yaml + implementation.py from the
#      harness template) and souls/<name>.md.
#   2. Creates .env from .env.example (if absent) and stamps
#      MIND_NAME / MIND_ID / MIND_SERVER_PORT.
#   3. Emits the installer for the chosen deployment into deploy/:
#      systemd unit, docker-compose.yml, or a Windows scheduled-task
#      PowerShell installer.
#
# It never runs sudo and never starts anything — it prints the install
# commands and leaves the moment to you.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

NAME="" PROFILE="" ROLE="" DEPLOYMENT="" HARNESS="" SURFACES="" PORT=""

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --name)       NAME="$2"; shift 2 ;;
        --profile)    PROFILE="$2"; shift 2 ;;
        --role)       ROLE="$2"; shift 2 ;;
        --deployment) DEPLOYMENT="$2"; shift 2 ;;
        --harness)    HARNESS="$2"; shift 2 ;;
        --surfaces)   SURFACES="$2"; shift 2 ;;
        --port)       PORT="$2"; shift 2 ;;
        -h|--help)    usage ;;
        *) echo "Unknown flag: $1" >&2; usage 1 ;;
    esac
done

ask() { # ask VAR "prompt" "default"
    local var="$1" prompt="$2" default="$3" answer
    if [ -z "${!var}" ]; then
        read -r -p "$prompt [$default]: " answer
        printf -v "$var" '%s' "${answer:-$default}"
    fi
}

ask NAME       "Mind name (lowercase, [a-z0-9_-])" "example"
if [ -z "$PROFILE" ] && [ ! -t 0 ]; then
    PROFILE="standard"
fi
ask PROFILE    "Profile: standard or sentinel" "standard"
ask ROLE       "Role: operator (full host access) or satellite" "satellite"
ask DEPLOYMENT "Deployment: systemd, container, or windows-task" "systemd"
ask HARNESS    "Harness: claude_cli_claude or codex_cli_codex" "claude_cli_claude"
ask SURFACES   "Surfaces (comma-separated: telegram,discord — or none)" "telegram"
ask PORT       "Mind server port" "8421"

case "$NAME" in
    (*[!a-z0-9_-]*|"") echo "Invalid name: '$NAME' (want [a-z0-9_-]+)" >&2; exit 1 ;;
esac
case "$PROFILE" in
    standard|sentinel) ;;
    *) echo "Invalid profile: '$PROFILE'" >&2; exit 1 ;;
esac
case "$ROLE" in
    operator|satellite) ;;
    *) echo "Invalid role: '$ROLE'" >&2; exit 1 ;;
esac
case "$DEPLOYMENT" in
    systemd|container|windows-task) ;;
    *) echo "Invalid deployment: '$DEPLOYMENT'" >&2; exit 1 ;;
esac
if [ ! -f "mind_templates/${HARNESS}.py" ]; then
    echo "Unknown harness: '$HARNESS' (no mind_templates/${HARNESS}.py)" >&2
    exit 1
fi
case "$PORT" in
    (*[!0-9]*|"") echo "Invalid port: '$PORT'" >&2; exit 1 ;;
esac

# The browser terminal runs each claude session inside tmux — a systemd
# claude mind without it fails at the worst place: the moment a tile opens.
if [ "$DEPLOYMENT" = "systemd" ] && [ "$HARNESS" = "claude_cli_claude" ] \
        && ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required for the browser terminal (apt install tmux)" >&2
    exit 1
fi

# An existing .env MIND_ID is this mind's identity — every memory write in
# the hive carries it. Reuse it; minting a second one would split the mind.
MIND_ID=""
if [ -f .env ]; then
    MIND_ID="$(sed -n 's/^MIND_ID=//p' .env | head -1)"
fi
if [ -z "$MIND_ID" ]; then
    MIND_ID="$(command -v uuidgen >/dev/null 2>&1 && uuidgen \
               || python3 -c 'import uuid; print(uuid.uuid4())')"
fi

# ---------------------------------------------------------------------------
# 1. Mind scaffold
# ---------------------------------------------------------------------------
if [ -d "minds/$NAME" ] && [ "$NAME" != "example" ]; then
    echo "minds/$NAME already exists — leaving it untouched."
else
    mkdir -p "minds/$NAME"
fi
[ -f "minds/$NAME/__init__.py" ] || : > "minds/$NAME/__init__.py"
if [ ! -f "minds/$NAME/implementation.py" ]; then
    sed "s/MIND_NAME/$NAME/g" "mind_templates/${HARNESS}.py" \
        > "minds/$NAME/implementation.py"
    echo "Wrote minds/$NAME/implementation.py (from $HARNESS)"
fi

SURFACES_YAML=""
if [ -n "$SURFACES" ] && [ "$SURFACES" != "none" ]; then
    IFS=',' read -r -a _surf <<< "$SURFACES"
    for s in "${_surf[@]}"; do
        SURFACES_YAML+="  - ${s// /}"$'\n'
    done
fi
if [ ! -f "minds/$NAME/runtime.yaml" ] || [ "$NAME" = "example" ]; then
    cat > "minds/$NAME/runtime.yaml" <<EOF
name: $NAME
mind_id: $MIND_ID
description: "$NAME — Hive Edge Mind."
profile: $PROFILE
role: $ROLE
deployment: $DEPLOYMENT
harness: $HARNESS
provider: anthropic
default_model: sonnet
mind_server_port: $PORT
surfaces:
${SURFACES_YAML:-  []}
soul_file: souls/$NAME.md
EOF
    echo "Wrote minds/$NAME/runtime.yaml"
fi
if [ ! -f "souls/$NAME.md" ]; then
    cp souls/example.md "souls/$NAME.md"
    echo "Wrote souls/$NAME.md — edit it before first registration."
fi

# ---------------------------------------------------------------------------
# 2. .env
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
fi
stamp_env() { # stamp_env KEY VALUE — replace or append
    local key="$1" value="$2"
    if grep -q "^${key}=" .env; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}
stamp_env MIND_NAME "$NAME"
grep -q "^MIND_ID=.\+" .env || stamp_env MIND_ID "$MIND_ID"
stamp_env MIND_SERVER_PORT "$PORT"
echo "Stamped .env (MIND_NAME, MIND_ID, MIND_SERVER_PORT)"

[ -f config.yaml ] || cp config.yaml.example config.yaml

# ---------------------------------------------------------------------------
# 3. Installer
# ---------------------------------------------------------------------------
mkdir -p deploy

emit_systemd() {
    # PATH for the unit: wherever the harness CLI actually resolves, plus the
    # standard system dirs. No hardcoded homes, no pinned node versions.
    local harness_bin="claude"
    [ "$HARNESS" = "codex_cli_codex" ] && harness_bin="codex"
    local bin_dir=""
    if command -v "$harness_bin" >/dev/null 2>&1; then
        bin_dir="$(dirname "$(command -v "$harness_bin")")"
    else
        echo "WARNING: '$harness_bin' not on PATH — install it, then re-run." >&2
        bin_dir="$HOME/.local/bin"
    fi
    local node_dir=""
    command -v node >/dev/null 2>&1 && node_dir="$(dirname "$(command -v node)")"
    local service_user="${USER:-$(id -un)}"
    local path_line="$bin_dir"
    [ -n "$node_dir" ] && [ "$node_dir" != "$bin_dir" ] && path_line+=":$node_dir"
    path_line+=":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    local service_user="${USER:-$(id -un)}"

    cat > "deploy/$NAME.service" <<EOF
[Unit]
Description=$NAME — Hive Edge Mind ($ROLE), bare-metal Linux service
After=network.target

[Service]
Type=simple
User=$service_user
WorkingDirectory=$ROOT
EnvironmentFile=$ROOT/.env
Environment=PATH=$path_line
ExecStart=$ROOT/.venv/bin/python3 launch_mind_server_and_bots.py
Restart=no
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
    echo "Wrote deploy/$NAME.service"

    if [ ! -x "$ROOT/.venv/bin/python3" ]; then
        echo "Building venv at $ROOT/.venv ..."
        python3 -m venv "$ROOT/.venv"
        "$ROOT/.venv/bin/pip" install --quiet --upgrade pip
        "$ROOT/.venv/bin/pip" install --quiet -r requirements.txt
    fi

    cat <<EOF

Next steps:
  1. Fill in .env (tokens) and config.yaml (allowlists).
  2. Edit souls/$NAME.md.
  3. Install and start:
       sudo cp deploy/$NAME.service /etc/systemd/system/$NAME.service
       sudo systemctl daemon-reload
       sudo systemctl start $NAME.service
  4. Register the mind with your hive (hive-mind repo: /add-mind).
EOF
}

emit_container() {
    local operator_block=""
    if [ "$ROLE" = "operator" ]; then
        operator_block="$(cat <<'YAML'
    # Operator posture: the entire host root filesystem, live read-write, at
    # /host — and the Docker socket through it. Deliberately un-hardened;
    # remove this mount (and switch role to satellite) if that is not what
    # you want this mind to be.
    volumes:
      - /:/host
YAML
)"
    else
        operator_block="$(cat <<'YAML'
    volumes:
      - .:/usr/src/app:rw
YAML
)"
    fi

    # Codex installs to a hivemind-owned NPM_CONFIG_PREFIX (see Dockerfile).
    # Mounting a named volume there lets every codex-harness container on
    # this host share one codex install: `npm install -g @openai/codex@latest`
    # run in any one of them updates it for all of them, no rebuild needed.
    # The volume is local to this host's Docker daemon on purpose — edge minds
    # on other hosts each get their own, updated separately (see
    # tools/stateless/update_codex/).
    local codex_volume_line="" codex_volumes_block=""
    if [ "$HARNESS" = "codex_cli_codex" ]; then
        codex_volume_line="      - codex-global:/home/hivemind/.npm-global
"
        codex_volumes_block="
volumes:
  codex-global:
    external: true
    name: codex-global
"
    fi

    cat > docker-compose.yml <<EOF
# $NAME — Hive Edge Mind ($ROLE) as a container.
# Publishes the Mind API on the selected host port. Hive service endpoints
# come from .env so this deployment also works on a different machine.
services:
  $NAME:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: hive-edge-mind-$NAME
    restart: unless-stopped
    working_dir: /usr/src/app
    env_file:
      - .env
    ports:
      - "$PORT:$PORT"
$operator_block
$codex_volume_line
    extra_hosts:
      - "host.docker.internal:host-gateway"
$codex_volumes_block
EOF
    echo "Wrote docker-compose.yml"
    if [ "$HARNESS" = "codex_cli_codex" ]; then
        cat <<EOF
Note: this mind shares codex updates with sibling codex-harness containers
on this host via the codex-global volume. Create it once per host before
first start:
  docker volume create codex-global
EOF
    fi
    cat <<EOF

Next steps:
  1. Fill in .env (tokens) and config.yaml (allowlists).
  2. Edit souls/$NAME.md.
  3. Build and start:  docker compose up -d --build
  4. Register the mind with your hive (hive-mind repo: /add-mind).
EOF
}

emit_windows_task() {
    cat > "deploy/setup-task.ps1" <<EOF
# Registers the $NAME Hive Edge Mind as a logon-triggered scheduled task.
# Run on the Windows box AS THE USER the mind should run under:
#   powershell -ExecutionPolicy Bypass -File deploy\\setup-task.ps1
\$ErrorActionPreference = "Stop"
\$root = Split-Path -Parent (Split-Path -Parent \$MyInvocation.MyCommand.Path)
\$pythonw = Join-Path \$root ".venv\\Scripts\\pythonw.exe"
if (-not (Test-Path \$pythonw)) {
    Write-Error "No venv at \$root\\.venv — run: python -m venv .venv; .venv\\Scripts\\pip install -r requirements.txt"
}
\$action = New-ScheduledTaskAction -Execute \$pythonw \`
    -Argument (Join-Path \$root "launch_windows.py") -WorkingDirectory \$root
\$trigger = New-ScheduledTaskTrigger -AtLogOn -User \$env:USERNAME
\$settings = New-ScheduledTaskSettingsSet \`
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries \`
    -StartWhenAvailable -MultipleInstances IgnoreNew \`
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) \`
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "${NAME}Bot" -Action \$action \`
    -Trigger \$trigger -Settings \$settings -Force
Write-Host "Registered scheduled task ${NAME}Bot (starts at logon of \$env:USERNAME)."
Write-Host "Start now with: schtasks /run /tn ${NAME}Bot"
EOF
    echo "Wrote deploy/setup-task.ps1"
    cat <<EOF

Next steps (on the Windows box):
  1. python -m venv .venv ; .venv\\Scripts\\pip install -r requirements.txt
  2. Fill in .env (tokens; WINDOWS_PATH_PREPEND if codex.exe is standalone)
     and config.yaml. Edit souls/$NAME.md.
  3. powershell -ExecutionPolicy Bypass -File deploy\\setup-task.ps1
  4. Register the mind with your hive (hive-mind repo: /add-mind).
EOF
}

case "$DEPLOYMENT" in
    systemd)      emit_systemd ;;
    container)    emit_container ;;
    windows-task) emit_windows_task ;;
esac
