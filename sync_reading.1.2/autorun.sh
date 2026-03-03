#!/bin/sh
set -eu

umask 077
PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP="sync_reading"
BASE="/opt/$APP"
LOGDIR="$BASE/logs"
RUNDIR="$BASE/run"
SPOOLDIR="$BASE/spool"
LOG="$LOGDIR/app.log"
PIDFILE="$RUNDIR/$APP.pid"
LOCKDIR="$RUNDIR/$APP.lockdir"

mkdir -p "$LOGDIR" "$RUNDIR" "$SPOOLDIR"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$LOG"; }

rotate_log() {
  MAX=5242880
  [ -f "$LOG" ] || return 0
  SIZE=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  if [ "$SIZE" -ge "$MAX" ]; then
    i=5
    while [ $i -ge 1 ]; do
      [ -f "$LOG.$i" ] && mv "$LOG.$i" "$LOG.$((i+1))" 2>/dev/null || true
      i=$((i-1))
    done
    mv "$LOG" "$LOG.1" 2>/dev/null || true
  fi
}

cleanup() {
  rotate_log
  log "signal received; stopping child..."
  if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null || true
      sleep 2
      kill -9 "$PID" 2>/dev/null || true
    fi
  fi
  rmdir "$LOCKDIR" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

# lock (evita múltiplas instâncias)
mkdir "$LOCKDIR" 2>/dev/null || { rotate_log; log "already running; exiting"; exit 0; }

# sanity checks
[ -f "$BASE/app.py" ] || { rotate_log; log "missing $BASE/app.py"; exit 1; }
command -v python3 >/dev/null 2>&1 || { rotate_log; log "python3 not found"; exit 1; }

# exporta paths pro app (evita hardcode)
export APP_BASE_DIR="$BASE"
export APP_CONFIG="${APP_CONFIG:-$BASE/config.json}"

rotate_log
log "==== START ===="
( whoami; pwd; ls -l "$BASE" ) >> "$LOG" 2>&1 || true

# supervisão: reinicia se cair
attempt=0
while true; do
  rotate_log
  log "starting python app (attempt=$attempt): $BASE/app.py"

  # Recomendado: app loga em stdout (jsonl); aqui capturamos no arquivo
  python3 -u "$BASE/app.py" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  chmod 600 "$PIDFILE" 2>/dev/null || true

  wait "$(cat "$PIDFILE")" || true
  attempt=$((attempt+1))
  log "app exited; restarting in 3s"
  sleep 3
done