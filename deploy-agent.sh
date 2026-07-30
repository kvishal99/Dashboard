#!/usr/bin/env bash
#
# Install / restart the PM2 reporting agent on a server.
#
#     ./deploy-agent.sh                      # default SSH_HOST from .env
#     ./deploy-agent.sh 3.94.49.56           # a specific server
#     ./deploy-agent.sh --stop 3.94.49.56    # remove the agent from that server
#
# The agent posts PM2 stats back to this dashboard. Because this machine has no
# public address, it posts to REMOTE_AGENT_PORT on the server's own localhost,
# which the reverse tunnel from run-with-tunnel.sh forwards back here. So:
#
#     run-with-tunnel.sh must be running for the agent to reach the dashboard.
#
# The agent is registered with pm2 under the name `ops-dashboard-agent`, so it
# survives reboots once you've run `pm2 save` on that server.

set -euo pipefail
cd "$(dirname "$0")"

# .env lives beside this script, or one level up when the code sits in a
# subdirectory (e.g. a git repo checked out as ./Dashboard) and .env is kept
# outside it. OPS_ENV_FILE overrides both.
ENV_FILE="${OPS_ENV_FILE:-}"
if [ -z "$ENV_FILE" ]; then
  if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then ENV_FILE=".env"
  elif [ -f ../.env ]; then ENV_FILE="../.env"
  fi
fi

STOP=0
if [ "${1:-}" = "--stop" ]; then STOP=1; shift; fi

if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      SSH_USER|SSH_HOST|SSH_PORT|SSH_PASSWORD|SSH_KEY|REMOTE_AGENT_PORT|OPS_AGENT_SECRET)
        value="${value%$'\r'}"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        [ -z "${!key:-}" ] && export "$key=$value"
        ;;
    esac
  done < <(grep -E "^[A-Z_]+=" "$ENV_FILE" || true)
fi

SSH_USER="${SSH_USER:-fcampbell}"
TARGET="${1:-${SSH_HOST:-44.198.210.209}}"
SSH_PORT="${SSH_PORT:-22}"

# Per-host overrides. Servers don't necessarily share one password or login, so
# for 3.94.49.56 you can set any of these in .env and they win for that host:
#     SSH_PASSWORD_3_94_49_56=...
#     SSH_USER_3_94_49_56=someoneelse
#     SSH_KEY_3_94_49_56=/home/vishal/.ssh/id_thatbox
HOST_SUFFIX="$(echo "$TARGET" | tr '.-' '__')"
for var in SSH_PASSWORD SSH_USER SSH_KEY SSH_PORT; do
  specific="${var}_${HOST_SUFFIX}"
  # Re-read .env for the host-specific key, since the loop above only picks up
  # the generic names.
  if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
    value="$(grep -E "^${specific}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"; value="${value%\"}"; value="${value#\"}"
    if [ -n "$value" ]; then
      export "$var=$value"
      echo "using $specific for $TARGET"
    fi
  fi
done
SSH_USER="${SSH_USER:-fcampbell}"
SSH_PORT="${SSH_PORT:-22}"
REMOTE_AGENT_PORT="${REMOTE_AGENT_PORT:-8777}"
APP_NAME="ops-dashboard-agent"
# Relative to the login home directory. scp does not shell-expand the remote
# path, so this must not contain $HOME or ~.
REMOTE_DIR="ops-dashboard-agent"

if [ -z "${OPS_AGENT_SECRET:-}" ]; then
  echo "OPS_AGENT_SECRET is not set in .env - the dashboard would reject the agent."
  exit 1
fi

ASKPASS=""
cleanup() { [ -n "$ASKPASS" ] && rm -f "$ASKPASS"; }
trap cleanup EXIT INT TERM

# Build the ssh/scp auth arguments once.
SSH_AUTH=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
if [ -n "${SSH_KEY:-}" ] && [ -f "${SSH_KEY:-}" ]; then
  SSH_AUTH+=(-i "$SSH_KEY" -o BatchMode=yes)
elif [ -n "${SSH_PASSWORD:-}" ]; then
  ASKPASS="$(mktemp /tmp/ops-askpass-XXXXXX.sh)"
  chmod 700 "$ASKPASS"
  printf '#!/usr/bin/env bash\nprintf %%s "$OPS_SSH_PASSWORD"\n' > "$ASKPASS"
  SSH_AUTH+=(-o PreferredAuthentications=password -o PubkeyAuthentication=no
             -o NumberOfPasswordPrompts=1)
  export OPS_SSH_PASSWORD="$SSH_PASSWORD"
  export SSH_ASKPASS="$ASKPASS"
  export SSH_ASKPASS_REQUIRE=force
  export DISPLAY="${DISPLAY:-:0}"
fi

run_remote() { ssh "${SSH_AUTH[@]}" -p "$SSH_PORT" "$SSH_USER@$TARGET" "$@"; }

if [ "$STOP" = "1" ]; then
  echo "Removing $APP_NAME from $TARGET ..."
  run_remote "pm2 delete $APP_NAME 2>/dev/null; pm2 save 2>/dev/null; echo removed"
  exit 0
fi

echo "Deploying agent to $SSH_USER@$TARGET ..."

# Ship agent.py, then (re)start it under pm2 with the environment it needs.
# --update-env makes pm2 pick up a changed secret or port on redeploy.
run_remote "mkdir -p ~/$REMOTE_DIR" >/dev/null
scp "${SSH_AUTH[@]}" -P "$SSH_PORT" -q agent.py "$SSH_USER@$TARGET:$REMOTE_DIR/agent.py"

run_remote "
  set -e
  cd ~/$REMOTE_DIR
  export SERVER_ID=\"\$(hostname)\"
  export DASHBOARD_URL='http://127.0.0.1:$REMOTE_AGENT_PORT/api/pm2/report'
  export AGENT_SECRET='$OPS_AGENT_SECRET'
  export INTERVAL_SECONDS=5
  pm2 delete $APP_NAME >/dev/null 2>&1 || true
  pm2 start agent.py --name $APP_NAME --interpreter python3 --update-env >/dev/null
  pm2 save >/dev/null 2>&1 || true
  sleep 6
  echo '--- agent log ---'
  pm2 logs $APP_NAME --lines 8 --nostream 2>/dev/null | tail -10
"

echo
echo "Deployed to $TARGET. The Processes tab should show it within ~10s,"
echo "as long as ./run-with-tunnel.sh is running."
