#!/usr/bin/env bash
#
# Run the dashboard against the MASTER database over an SSH tunnel.
#
#     ./run-with-tunnel.sh
#
# Put SSH_PASSWORD in .env and this is fully automatic - no prompts. If
# SSH_PASSWORD isn't set, it falls back to asking you interactively.
#
# Why a tunnel: port 3306 on the master is firewalled from this machine, but
# SSH (port 22) is open. The tunnel forwards localhost:3307 to the master's own
# 127.0.0.1:3306, so MySQL sees a purely local connection.
#
# The password is handed to ssh through an SSH_ASKPASS helper (OpenSSH >= 8.4).
# It is written to a private temp file that is deleted on exit, and it is never
# passed as a command-line argument - argv is visible to every user on the box
# via `ps`, which is exactly how `sshpass -p` leaks secrets.

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

# ---------------------------------------------------------------- settings

# Load .env without exporting anything unexpected: only the keys we use.
if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      SSH_USER|SSH_HOST|SSH_PORT|SSH_PASSWORD|SSH_KEY|LOCAL_PORT|APP_PORT|REMOTE_AGENT_PORT|OPS_AGENT_SECRET)
        # Strip surrounding quotes and any trailing CR from CRLF files.
        value="${value%$'\r'}"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        # Environment always wins over .env.
        [ -z "${!key:-}" ] && export "$key=$value"
        ;;
    esac
  done < <(grep -E "^[A-Z_]+=" "$ENV_FILE" || true)
fi

SSH_USER="${SSH_USER:-fcampbell}"
SSH_HOST="${SSH_HOST:-100.52.8.134}"
SSH_PORT="${SSH_PORT:-22}"
LOCAL_PORT="${LOCAL_PORT:-3307}"
APP_PORT="${APP_PORT:-8000}"
# Port opened ON THE SERVER that forwards back to this machine's dashboard, so
# agent.py there can POST its PM2 stats to a laptop that has no public address.
REMOTE_AGENT_PORT="${REMOTE_AGENT_PORT:-8777}"
# The Monday status sheet, used to populate the "In partner feed" column.
PARTNER_CSV="${OPS_PARTNER_CSV:-/home/vishal/Downloads/Monday Partner Status - Final.csv}"

CONTROL="$(mktemp -u /tmp/ops-tunnel-XXXXXX.sock)"
ASKPASS=""

cleanup() {
  [ -n "$ASKPASS" ] && rm -f "$ASKPASS"
  if [ -S "$CONTROL" ]; then
    echo
    echo "Closing SSH tunnel..."
    ssh -S "$CONTROL" -O exit "$SSH_USER@$SSH_HOST" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ------------------------------------------------------------ sanity check

# Is a dashboard already serving? If so this is a LIVE session - someone is
# using it in a browser right now - and we must not disturb it or the tunnel it
# depends on. Checked before anything gets killed.
DASHBOARD_LIVE=0
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$APP_PORT/api/jobs" 2>/dev/null; then
  DASHBOARD_LIVE=1
fi

if [ "$DASHBOARD_LIVE" = "1" ]; then
  echo "A dashboard is already running and responding on http://127.0.0.1:$APP_PORT"
  echo
  echo "Nothing was changed - stopping it here would kill a session you may be"
  echo "using, along with the tunnel it depends on."
  echo
  echo "  - to keep using it:   just open http://127.0.0.1:$APP_PORT"
  echo "  - to restart it:      press Ctrl-C in the terminal running it, then rerun"
  echo "  - to run a second:    APP_PORT=8001 LOCAL_PORT=3308 ./run-with-tunnel.sh"
  exit 0
fi

if ss -ltn 2>/dev/null | grep -q ":$LOCAL_PORT " ; then
  # No dashboard is serving, so a tunnel still holding this port is left over
  # from a run whose trap never fired (Ctrl-C elsewhere, closed shell, crash).
  # Safe to take over now that we know nothing is using it.
  OWN_PIDS="$(pgrep -f "ssh -M -S /tmp/ops-tunnel.*-L $LOCAL_PORT:" 2>/dev/null || true)"
  if [ -n "$OWN_PIDS" ]; then
    echo "Port $LOCAL_PORT was held by an earlier run of this script - closing it."
    for p in $OWN_PIDS; do kill "$p" 2>/dev/null || true; done
    rm -f /tmp/ops-tunnel-*.sock
    for _ in 1 2 3 4 5; do
      ss -ltn 2>/dev/null | grep -q ":$LOCAL_PORT " || break
      sleep 1
    done
  fi

  # Still busy: something we don't recognise owns it, so don't touch it.
  if ss -ltn 2>/dev/null | grep -q ":$LOCAL_PORT " ; then
    echo "Port $LOCAL_PORT is in use by something this script did not start:"
    ss -ltnp 2>/dev/null | grep ":$LOCAL_PORT " | sed 's/^/   /'
    echo
    echo "Either stop it, or run on a different port:"
    echo "   LOCAL_PORT=3308 ./run-with-tunnel.sh"
    exit 1
  fi
fi

# The dashboard port is never force-taken. We already returned above if one was
# actually serving; anything still holding the port here is not answering, so
# report it and let the user decide rather than killing a process blindly.
if ss -ltn 2>/dev/null | grep -q ":$APP_PORT " ; then
  echo "Port $APP_PORT is in use but not answering as a dashboard:"
  ss -ltnp 2>/dev/null | grep ":$APP_PORT " | sed 's/^/   /'
  echo
  echo "Stop it, or run on another port:  APP_PORT=8001 ./run-with-tunnel.sh"
  exit 1
fi

# ---------------------------------------------------------------- the tunnel

echo "Opening SSH tunnel: localhost:$LOCAL_PORT -> $SSH_HOST's 127.0.0.1:3306"

SSH_OPTS=(
  -M -S "$CONTROL" -f -N
  -p "$SSH_PORT"
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
  # Forward only: our 3307 -> the server's MySQL. This one is critical, so
  # ExitOnForwardFailure applies to it and nothing else.
  #
  # The reverse forward (-R, for agent.py reporting back) is deliberately NOT
  # here. It goes on a SEPARATE connection below, because a leftover listener
  # on the remote port would otherwise fail the whole tunnel under
  # ExitOnForwardFailure and take MySQL down with it - which is exactly the
  # "remote port forwarding failed for listen port 8777" case.
  -L "$LOCAL_PORT:127.0.0.1:3306"
)

if [ -n "${SSH_KEY:-}" ] && [ -f "${SSH_KEY:-}" ]; then
  # Key auth - no secret on disk beyond the key itself. See ./setup-ssh-key.sh
  echo "Authenticating with SSH key $SSH_KEY ..."
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -o BatchMode=yes "$SSH_USER@$SSH_HOST"

elif [ -n "${SSH_PASSWORD:-}" ]; then
  echo "Authenticating with SSH_PASSWORD from .env..."
  ASKPASS="$(mktemp /tmp/ops-askpass-XXXXXX.sh)"
  chmod 700 "$ASKPASS"
  # The helper prints the password on stdout. It reads it from its own
  # environment, so the secret never appears in this script's argv.
  printf '#!/usr/bin/env bash\nprintf %%s "$OPS_SSH_PASSWORD"\n' > "$ASKPASS"

  # PreferredAuthentications: skip straight to password auth, otherwise ssh
  # burns its attempts on publickey/gssapi first and can hit MaxAuthTries.
  OPS_SSH_PASSWORD="$SSH_PASSWORD" \
  SSH_ASKPASS="$ASKPASS" \
  SSH_ASKPASS_REQUIRE=force \
  DISPLAY="${DISPLAY:-:0}" \
    ssh "${SSH_OPTS[@]}" \
        -o PreferredAuthentications=password \
        -o PubkeyAuthentication=no \
        -o NumberOfPasswordPrompts=1 \
        "$SSH_USER@$SSH_HOST"
else
  echo "SSH_PASSWORD not set in .env - you'll be prompted."
  ssh "${SSH_OPTS[@]}" "$SSH_USER@$SSH_HOST"
fi

echo "Tunnel is up."
echo

# ------------------------------------------------- reverse tunnels, extra hosts
#
# The reverse forward above only exists on THIS ssh connection, so an agent on
# any other server has no route back to this laptop. Open a reverse-only tunnel
# per extra host listed in AGENT_HOSTS (space or comma separated) in .env, e.g.
#     AGENT_HOSTS=3.94.49.56 34.197.195.248
# Per-host credentials work the same as in deploy-agent.sh:
#     SSH_PASSWORD_3_94_49_56=...
EXTRA_CONTROLS=()
cleanup_extras() {
  for c in "${EXTRA_CONTROLS[@]:-}"; do
    [ -n "$c" ] && [ -S "$c" ] && ssh -S "$c" -O exit x 2>/dev/null || true
  done
}
trap 'cleanup_extras; cleanup' EXIT INT TERM

# open_reverse <host> <user> <password> <key>
# Best-effort: a failure here is reported and skipped. It never aborts the run,
# because the Partners and Website Health tabs don't need it at all - only the
# Processes tab does.
open_reverse() {
  local host="$1" user="$2" pass="$3" key="$4"
  local ctl; ctl="$(mktemp -u /tmp/ops-tunnel-rev-XXXXXX.sock)"
  local opts=(-M -S "$ctl" -f -N -o ExitOnForwardFailure=yes
              -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new
              -o ConnectTimeout=15 -R "$REMOTE_AGENT_PORT:127.0.0.1:$APP_PORT")
  local err
  if [ -n "$key" ] && [ -f "$key" ]; then
    err="$(ssh "${opts[@]}" -i "$key" -o BatchMode=yes "$user@$host" 2>&1)" || true
  elif [ -n "$pass" ]; then
    local ask; ask="$(mktemp /tmp/ops-askpass-XXXXXX.sh)"; chmod 700 "$ask"
    printf '#!/usr/bin/env bash\nprintf %%s "$OPS_SSH_PASSWORD"\n' > "$ask"
    err="$(OPS_SSH_PASSWORD="$pass" SSH_ASKPASS="$ask" SSH_ASKPASS_REQUIRE=force \
           DISPLAY="${DISPLAY:-:0}" \
           ssh "${opts[@]}" -o PreferredAuthentications=password \
               -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 \
               "$user@$host" 2>&1)" || true
    rm -f "$ask"
  else
    echo "SKIPPED - no password or key configured"
    return 0
  fi

  if [ -S "$ctl" ]; then
    echo "up"
    EXTRA_CONTROLS+=("$ctl")
  elif echo "$err" | grep -q "remote port forwarding failed"; then
    # Almost always a previous run of this script that is still connected, or
    # an ssh the server hasn't reaped yet. The agent keeps posting to that
    # older tunnel, so process data still flows - just not through this run.
    echo "port $REMOTE_AGENT_PORT already bound on $host"
    echo "      (another tunnel is still up, or a stale one - the Processes tab"
    echo "       may still work through it. To take it over:"
    echo "       ssh $user@$host \"fuser -k $REMOTE_AGENT_PORT/tcp\"  then rerun,"
    echo "       or start this script with REMOTE_AGENT_PORT=8778)"
  else
    echo "FAILED - $(echo "$err" | tail -1)"
  fi
}

# The reverse tunnel for the main host, on its own connection.
echo -n "Reverse tunnel to $SSH_HOST (agent reporting) ... "
open_reverse "$SSH_HOST" "$SSH_USER" "${SSH_PASSWORD:-}" "${SSH_KEY:-}"
echo

if [ -n "${AGENT_HOSTS:-}" ]; then
  for host in ${AGENT_HOSTS//,/ }; do
    [ "$host" = "$SSH_HOST" ] && continue
    suffix="$(echo "$host" | tr '.-' '__')"
    h_user="$(grep -E "^SSH_USER_${suffix}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
    h_pass="$(grep -E "^SSH_PASSWORD_${suffix}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
    h_key="$(grep -E "^SSH_KEY_${suffix}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
    h_user="${h_user:-$SSH_USER}"
    echo -n "Reverse tunnel to $host ... "
    open_reverse "$host" "$h_user" "$h_pass" "$h_key"
  done
  echo
fi

# ------------------------------------------------------------- verify + run

export OPS_DB_HOST=127.0.0.1
export OPS_DB_PORT="$LOCAL_PORT"

echo "Verifying which database answered..."
if ! ./venv/bin/python check_db.py active; then
  echo
  echo "The tunnel is open but the database check failed - see the error above."
  exit 1
fi

# Fill the "In partner feed" column from the status sheet if nothing has
# reported those numbers yet. Writes straight to ops.db, so it runs BEFORE the
# dashboard starts - going via the HTTP API used to race startup and fail
# silently. Skipped when live figures are already present.
if [ -f "$PARTNER_CSV" ] || [ -f "${OPS_PARTNER_CSV:-}" ]; then
  echo "Filling partner-feed counts from the status sheet..."
  ./venv/bin/python import-feed-counts.py 2>&1 | tail -1
  echo
fi

echo "Starting dashboard on http://127.0.0.1:$APP_PORT  (Ctrl-C to stop)"
echo
exec ./venv/bin/python -m uvicorn dashboard:app --host 127.0.0.1 --port "$APP_PORT"
