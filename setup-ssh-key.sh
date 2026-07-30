#!/usr/bin/env bash
#
# One-time: install an SSH key on the master so the tunnel no longer needs a
# password at all.
#
#     ./setup-ssh-key.sh
#
# Uses SSH_PASSWORD from .env once (or prompts), copies a dedicated key up,
# verifies key-only login works, and then tells you to blank SSH_PASSWORD.
#
# This is strictly better than keeping the password in .env: the private key
# stays on this machine, is protected by file permissions, and can be revoked
# on the server by deleting one line from authorized_keys.

set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      SSH_USER|SSH_HOST|SSH_PORT|SSH_PASSWORD)
        value="${value%$'\r'}"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        [ -z "${!key:-}" ] && export "$key=$value"
        ;;
    esac
  done < <(grep -E '^[A-Z_]+=' .env || true)
fi

SSH_USER="${SSH_USER:-fcampbell}"
SSH_HOST="${SSH_HOST:-44.198.210.209}"
SSH_PORT="${SSH_PORT:-22}"
KEY="$HOME/.ssh/ops_dashboard_${SSH_HOST//./_}"

ASKPASS=""
cleanup() { [ -n "$ASKPASS" ] && rm -f "$ASKPASS"; }
trap cleanup EXIT INT TERM

if [ ! -f "$KEY" ]; then
  echo "Generating a dedicated key at $KEY ..."
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "ops-dashboard tunnel $(hostname)"
else
  echo "Reusing existing key $KEY"
fi

echo
echo "Installing the public key on $SSH_USER@$SSH_HOST ..."

if [ -n "${SSH_PASSWORD:-}" ]; then
  ASKPASS="$(mktemp /tmp/ops-askpass-XXXXXX.sh)"
  chmod 700 "$ASKPASS"
  printf '#!/usr/bin/env bash\nprintf %%s "$OPS_SSH_PASSWORD"\n' > "$ASKPASS"
  OPS_SSH_PASSWORD="$SSH_PASSWORD" SSH_ASKPASS="$ASKPASS" \
  SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-:0}" \
    ssh-copy-id -i "$KEY.pub" -p "$SSH_PORT" \
        -o PreferredAuthentications=password \
        -o PubkeyAuthentication=no \
        -o StrictHostKeyChecking=accept-new \
        "$SSH_USER@$SSH_HOST"
else
  echo "(SSH_PASSWORD not set - you'll be prompted once)"
  ssh-copy-id -i "$KEY.pub" -p "$SSH_PORT" \
      -o StrictHostKeyChecking=accept-new "$SSH_USER@$SSH_HOST"
fi

echo
echo "Verifying key-only login (password auth disabled for this test)..."
if ssh -i "$KEY" -p "$SSH_PORT" -o BatchMode=yes -o PasswordAuthentication=no \
       -o ConnectTimeout=15 "$SSH_USER@$SSH_HOST" "echo OK; hostname"; then
  echo
  echo "Done. Key auth works."
  echo
  echo "Now do two things:"
  echo "  1. Blank the password line in .env:   SSH_PASSWORD="
  echo "  2. Add this line to .env so the tunnel uses the key:"
  echo "       SSH_KEY=$KEY"
  echo
  echo "Then ./run-with-tunnel.sh runs with no password anywhere on disk."
else
  echo
  echo "Key login did not work. The server may disallow key auth, or"
  echo "authorized_keys may have the wrong permissions. Keep using SSH_PASSWORD."
  exit 1
fi
