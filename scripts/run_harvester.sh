#!/usr/bin/env bash
#
# Regularized start/stop for the HARVESTER (scripts/harvest.py --run) — the signature
# collector, which runs for weeks and has to outlive the terminal that started it. It gets
# the same launcher shape as the align server (scripts/run_align.sh): its own pidfile, its
# own log with a size cap, and a start that refuses to put a second harvester on the same
# state directory.
#
#   scripts/run_harvester.sh start      # harvest.py --run under the venv, MallocLargeCache=0
#   scripts/run_harvester.sh stop       # SIGTERM, then WAIT for the exit
#   scripts/run_harvester.sh restart
#   scripts/run_harvester.sh status     # up or down, the pid, the phase, the ledger
#   scripts/run_harvester.sh help
#
# Why a launcher at all, when `make harvest-run` already ran it: that target ran in the
# FOREGROUND, so a run of several weeks died with the terminal, nothing on disk said which
# process it was, and .harvest/harvest.log grew without a bound. `make harvest-run` is now
# an alias for `start`, so there is one way in and one pidfile whoever starts it.
#
# ONE HARVESTER AT A TIME. Two would interleave writes to .harvest/queue.json and
# .harvest/state.json. harvest.py enforces that itself with a flock it holds for its
# lifetime (harvest.WRITER_LOCK), so a second one refuses rather than corrupting anything;
# `start` refuses earlier and more cheaply, by looking for the process it launched before.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

load_env() {
  # The repo's .env, read the way every make target reads it: the file wins. An ABSENT
  # .env is a normal state, not an error — a fresh clone has none and every value below
  # has a default — so this returns quietly.
  [ -f "$ROOT/.env" ] || return 0
  set -a
  # shellcheck disable=SC1091   # machine-local, never committed
  . "$ROOT/.env"
  set +a
}
load_env

STATE_DIR="$ROOT/.harvest"
STATE="$STATE_DIR/state.json"
LEDGER="$STATE_DIR/ledger.json"
PIDFILE="$STATE_DIR/harvester.pid"          # this launcher's own; nothing else writes it
LOG="$STATE_DIR/harvest.log"
# The log rotation, one generation: over the cap, harvest.log becomes harvest.log.1 and a
# fresh harvest.log is opened. 10 MB because the log is a narrative read after the fact,
# not a data set — two generations of it answer "what happened last night" and nothing
# larger ever gets read.
LOG_MAX_BYTES="${NETRADIO_HARVEST_LOG_MAX_BYTES:-10485760}"
# How long `stop` waits for a clean exit before it stops being polite. harvest.py handles
# SIGTERM itself — it finishes the candidate in hand and writes the state — so the wait is
# the point of `stop`, not a formality.
STOP_WAIT_S="${NETRADIO_HARVEST_STOP_WAIT_S:-30}"
PYTHON="${NETRADIO_PYTHON:-$ROOT/.venv/bin/python}"

is_harvester() {
  # $1 = pid. A live process is only OUR process if its command line still names the
  # script. Pids are reused, and a pidfile outlives a reboot; without this check a
  # recycled pid reads as a running harvester and `start` refuses forever.
  ps -p "$1" -o command= 2>/dev/null | grep -q 'harvest\.py'
}

running_pid() {
  # Echoes the live harvester's pid, or nothing at all. Never fails: "no harvester" is an
  # answer, not an error.
  [ -f "$PIDFILE" ] || return 0
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  case "$pid" in ''|*[!0-9]*) return 0 ;; esac
  if is_harvester "$pid"; then printf '%s' "$pid"; fi
  return 0
}

state_phase() {
  # The phase the harvester last wrote to .harvest/state.json ("working", "idle",
  # "waiting on <host>", "halted", ...). Read with grep rather than a JSON parser so
  # `status` answers with no venv and no interpreter of any kind.
  [ -f "$STATE" ] || { printf 'unknown (no state file yet)'; return 0; }
  local hit
  hit="$(grep -o -m1 '"phase"[[:space:]]*:[[:space:]]*"[^"]*"' "$STATE" 2>/dev/null || true)"
  [ -n "$hit" ] || { printf 'unknown'; return 0; }
  printf '%s' "$hit" | sed 's/.*"\([^"]*\)"$/\1/'
}

ledger_line() {
  # THE LEDGER'S ABSENCE IS NORMAL, AND IS NOT AN ERROR ANYWHERE IN THIS SCRIPT. The
  # ledger is written by the signing pass; before that pass has ever run there is no file,
  # and the honest reading of that is "nothing signed yet, no candidates" — not a fault,
  # not a reason to refuse a start, not a non-zero exit. A launcher that treated a missing
  # ledger as a failure would make a repo that has never run the harvester unstartable.
  [ -f "$LEDGER" ] || { printf 'absent (nothing signed yet)'; return 0; }
  printf 'present (%s bytes)' "$(wc -c <"$LEDGER" | tr -d ' ')"
}

rotate_log() {
  # One line, one generation, checked before the log is opened for append.
  [ -f "$LOG" ] || return 0
  local size
  size="$(wc -c <"$LOG" | tr -d ' ')"
  [ "$size" -ge "$LOG_MAX_BYTES" ] || return 0
  echo "rotating $LOG ($size bytes >= $LOG_MAX_BYTES) -> $LOG.1"
  mv -f "$LOG" "$LOG.1"
}

cmd_start() {
  local pid
  pid="$(running_pid)"
  if [ -n "$pid" ]; then
    echo "harvester already running (pid $pid) — leaving it alone" >&2
    return 1
  fi
  if [ -f "$PIDFILE" ]; then
    # A stale pidfile: the pid is gone, or belongs to something else now. Clear it and
    # carry on — this is the ordinary aftermath of a crash or a reboot.
    echo "clearing a stale pidfile ($PIDFILE)"
    rm -f "$PIDFILE"
  fi
  if [ ! -x "$PYTHON" ]; then
    echo "no interpreter at $PYTHON — run: make venv" >&2
    return 1
  fi
  mkdir -p "$STATE_DIR"
  rotate_log
  echo "starting the harvester (interpreter: $PYTHON)"
  # MallocLargeCache=0 tells macOS not to keep freed large blocks inside the process.
  # Without it the footprint only ever goes up: the run frees everything after each
  # candidate, libmalloc holds the pages anyway, and under pressure they are compressed
  # and swapped. It has to be in the environment AT PROCESS START, which is why it is set
  # here on the command rather than anywhere inside the run. Harmless off macOS (an
  # unknown variable). The fetch child sets it again for itself.
  MallocLargeCache=0 nohup "$PYTHON" scripts/harvest.py --run >>"$LOG" 2>&1 &
  pid=$!
  echo "$pid" >"$PIDFILE"
  sleep 1
  if ! is_harvester "$pid"; then
    echo "the harvester exited immediately — see the end of $LOG" >&2
    rm -f "$PIDFILE"
    return 1
  fi
  cmd_status
}

cmd_stop() {
  local pid
  pid="$(running_pid)"
  if [ -z "$pid" ]; then
    rm -f "$PIDFILE"
    echo "harvester not running"
    return 0
  fi
  echo "stopping the harvester (pid $pid), waiting up to ${STOP_WAIT_S}s for a clean exit"
  kill "$pid" 2>/dev/null || true
  local i
  for ((i = 0; i < STOP_WAIT_S * 2; i++)); do
    is_harvester "$pid" || break
    sleep 0.5
  done
  if is_harvester "$pid"; then
    echo "still up after ${STOP_WAIT_S}s — force-killing $pid" >&2
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PIDFILE"
  echo "stopped"
}

cmd_status() {
  local pid
  pid="$(running_pid)"
  if [ -n "$pid" ]; then
    echo "harvester UP (pid $pid)"
  else
    echo "harvester DOWN"
  fi
  echo "  phase:  $(state_phase)"
  echo "  ledger: $(ledger_line)"
  echo "  log:    $LOG"
  [ -n "$pid" ]
}

usage() {
  echo "usage: $0 {start|stop|restart|status|help}"
}

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status)  cmd_status ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
