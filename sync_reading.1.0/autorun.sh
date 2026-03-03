#!/bin/sh
set -eu

APP="sync_reading"
BASE="/opt/$APP"
LOGDIR="$BASE/logs"
RUNDIR="$BASE/run"
LOG="$LOGDIR/app.log"
PIDFILE="$RUNDIR/$APP.pid"

mkdir -p "$LOGDIR" "$RUNDIR"

log() {
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*" >> "$LOG"
}

rotate_log() {
  # Rotação simples: quando passar de ~5MB, mantém 5 arquivos
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

# Log de diagnóstico inicial (uma vez)
rotate_log
log "==== START ===="
( whoami; pwd; ls -l "$BASE" ) >> "$LOG" 2>&1 || true

# Supervisão simples: se cair, reinicia
while true; do
  rotate_log
  log "starting python app: $BASE/app.py"

  python3 -u "$BASE/app.py" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"

  wait "$(cat "$PIDFILE")" || true
  log "app exited; restarting in 3s"
  sleep 3
done