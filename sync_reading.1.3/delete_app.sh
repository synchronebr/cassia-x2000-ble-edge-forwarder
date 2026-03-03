#!/bin/sh
set -eu

APP="sync_reading"
BASE="/opt/$APP"
PIDFILE="$BASE/run/$APP.pid"

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE" || true)"
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
  fi
fi

# fallback
pkill -f "$BASE/app.py" 2>/dev/null || true

# se sua intenção é remover tudo ao desinstalar:
rm -rf "$BASE" 2>/dev/null || true
exit 0