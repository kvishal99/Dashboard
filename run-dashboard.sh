#!/usr/bin/env bash
#
# Start the dashboard if it isn't already running, and detach it from this
# terminal. For when you have no root and so cannot install the systemd unit.
#
#     ./run-dashboard.sh          start if not running (safe to repeat)
#     ./run-dashboard.sh status   is it up, and on what PID
#     ./run-dashboard.sh stop     stop it
#     ./run-dashboard.sh restart  stop, then start
#
# Because starting is a no-op when it's already up, the same command works as
# both a boot hook and a watchdog. Put BOTH lines in your own crontab -
# no sudo needed, and it then survives logout, crashes and reboots:
#
#     @reboot         /home/fcampbell/ops-dashboard/Dashboard/run-dashboard.sh
#     */5 * * * *     /home/fcampbell/ops-dashboard/Dashboard/run-dashboard.sh
#
# systemd is still the better answer if anyone with root will install
# ops-dashboard.service - it gives proper logging and supervision.

set -euo pipefail
cd "$(dirname "$0")"

APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-5603}"
LOG="${OPS_LOG:-$PWD/dashboard.log}"
# The hourly count sweep fires on the top of the LOCAL hour, so a UTC server
# would otherwise run it at :30 IST.
export TZ="${TZ:-Asia/Kolkata}"

PYTHON="$PWD/venv/bin/python"
PIDFILE="${OPS_PIDFILE:-$PWD/dashboard.pid}"

# A pidfile, not `pgrep -f`: pattern matching also finds any OTHER process whose
# command line happens to contain the pattern - an editor, a `ps | grep`, the
# watchdog's own shell - and a supervisor that mistakes those for a live server
# will refuse to start, or "stop" something that was never running.
running_pid() {
  [ -f "$PIDFILE" ] || return 0
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  case "$pid" in ''|*[!0-9]*) return 0 ;; esac
  kill -0 "$pid" 2>/dev/null || return 0
  # Confirm the pid is still OUR server and not a recycled number.
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "uvicorn dashboard:app" || return 0
  echo "$pid"
}

stop_it() {
  pid="$(running_pid || true)"
  if [ -z "$pid" ]; then echo "not running"; return 0; fi
  kill "$pid" 2>/dev/null || true
  # Generous: a graceful shutdown waits for in-flight work, and the count sweep
  # can hold a MySQL query for over a minute. Only force it after that.
  for _ in $(seq 1 60); do
    sleep 0.5
    [ -z "$(running_pid || true)" ] && { rm -f "$PIDFILE"; echo "stopped $pid"; return 0; }
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "force-stopped $pid"
}

case "${1:-start}" in
  status)
    pid="$(running_pid || true)"
    if [ -n "$pid" ]; then
      echo "running (pid $pid) on $APP_HOST:$APP_PORT"
      curl -s -o /dev/null -w "http %{http_code}\n" \
        "http://$APP_HOST:$APP_PORT/api/jobs" 2>/dev/null || echo "not answering yet"
    else
      echo "not running"; exit 1
    fi
    exit 0 ;;
  stop)    stop_it; exit 0 ;;
  restart) stop_it ;;
  start)   ;;
  *) echo "usage: $0 [start|stop|restart|status]" >&2; exit 2 ;;
esac

if [ -n "$(running_pid || true)" ]; then
  exit 0            # already up - this is the watchdog's normal case
fi

if [ ! -x "$PYTHON" ]; then
  echo "$PYTHON not found - build the venv with python3.12 -m venv venv" >&2
  exit 1
fi

# Keep the log from growing without bound; there is no logrotate for us here.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 52428800 ]; then
  mv -f "$LOG" "$LOG.1"
fi

# setsid detaches from the terminal, so it survives logout and closing the SSH
# session; nohup alone would not be enough once the session's tty disappears.
#
# The pid is written by the child itself, then exec'd over. Taking $! instead
# would be wrong under job control, where setsid forks and $! is the parent that
# immediately exits.
setsid nohup bash -c '
  echo $$ > "$1"
  exec "$2" -m uvicorn dashboard:app --host "$3" --port "$4"
' _ "$PIDFILE" "$PYTHON" "$APP_HOST" "$APP_PORT" >> "$LOG" 2>&1 &

for _ in $(seq 1 20); do
  sleep 0.5
  pid="$(running_pid || true)"
  [ -n "$pid" ] && { echo "started (pid $pid) -> $LOG"; exit 0; }
done
echo "failed to start - see $LOG" >&2
tail -20 "$LOG" >&2 || true
exit 1
