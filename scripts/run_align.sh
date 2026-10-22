#!/usr/bin/env bash
#
# Regularized start/stop for the /align inspector server (DP-17) — the align tool is
# its own process by design (scripts/align_server.py, default 127.0.0.1:8766; a reverse
# proxy can forward /align + /api/align/ to it from another host), so it gets its own
# launcher, the shape scripts/run_harvester.sh has too: own pidfile, own log under
# logs/, and the port cleared of stale processes before every (re)start.
#
#   scripts/run_align.sh start      # launch scripts/align_server.py (env from .env)
#   scripts/run_align.sh stop       # stop it + clear the port
#   scripts/run_align.sh restart
#   scripts/run_align.sh status     # is it up, on which URL, and is the DSP configured
#   scripts/run_align.sh running    # quiet: exit 0 up, 1 down (for a caller that only needs to know)
#   scripts/run_align.sh running-here  # quiet: 0 this checkout's server, 1 not (or nothing), 2 cannot tell
#   scripts/run_align.sh help       # usage + the where-does-the-data-come-from primer
#
# Exposure stays scripts/align_server.py's own: loopback unless NETRADIO_ALIGN_HOST says
# otherwise, Basic Auth when NETRADIO_AUTH_USER/PASS are set. This script never
# changes that posture — it only starts/stops the process.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/lib/env.sh
. "$ROOT/scripts/lib/env.sh"
# shellcheck source=scripts/lib/checkout.sh
. "$ROOT/scripts/lib/checkout.sh"

# The venv's interpreter, as scripts/run_harvester.sh uses: a bare `python3.14` on PATH can be an
# asdf shim, which exits at once in this checkout because it pins no Python version, and the
# server died before it bound the port (2026-10-21).
PYTHON="${NETRADIO_PYTHON:-$ROOT/.venv/bin/python}"
# the port: real env wins, else .env (align_server.py reads the same chain), else 8766
NETRADIO_ALIGN_PORT="$(env_get "$ROOT/.env" NETRADIO_ALIGN_PORT)"
PORT="${NETRADIO_ALIGN_PORT:-8766}"
# the log: real env wins, else .env, else logs/align.log in this checkout
NETRADIO_ALIGN_LOG="$(env_get "$ROOT/.env" NETRADIO_ALIGN_LOG)"
LOG="${NETRADIO_ALIGN_LOG:-$ROOT/logs/align.log}"
PIDFILE="$ROOT/.align.pid"

port_pids() { lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true; }

clear_port() {   # a stale process from an earlier run may still hold the port: clear it first
  local pids; pids="$(port_pids)"
  if [ -n "$pids" ]; then
    echo "clearing :$PORT (pids: $pids)"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    pids="$(port_pids)"
    # shellcheck disable=SC2086
    [ -n "$pids" ] && { echo "force-killing $pids"; kill -9 $pids 2>/dev/null || true; sleep 1; }
  fi
  rm -f "$PIDFILE"
}

launch() {
  clear_port
  mkdir -p "$(dirname "$LOG")"
  echo "starting align inspector on :$PORT (python: $PYTHON)"
  nohup "$PYTHON" scripts/align_server.py >| "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 20); do
    [ -n "$(port_pids)" ] && break
    sleep 0.5
  done
  if [ -z "$(port_pids)" ]; then
    echo "align inspector did not start; the end of $LOG:"
    tail -n 20 "$LOG" 2>/dev/null | sed 's/^/  /'
    return 1
  fi
  status
}

data_primer() {
  # The where-does-the-data-come-from primer (2026-08-03): shown on every
  # start and via `help`, because a freshly-started inspector with no hints files is
  # a correctly-running EMPTY page — the fix is running the converter, not debugging.
  cat <<PRIMER

Empty page? The inspector feeds on *.match.hints.tsv files in the labels dir
(NETRADIO_LABELS_DIR, else labels/). Generate them with the Pass-1 converter, then reload:

  cd $ROOT
  . .venv/bin/activate && NETRADIO_AUDIO_DIR=jaz_links python -m streamalign \\
      match-hints <stem> <NNN>              # one capture+original pair
  #   match-hints <stem> --all              # every original overlapping the capture
  #   match-hints <stem> <NNN> --around S   # track beyond the placed region (~min 375+)

The files land in labels/automated/ (committed machine output since DP-24) — invisible
to the label pipeline and the publish flow until you fold accepted points in (Audacity
import, or the page's export button).
PRIMER
}

env_or_dotenv() {   # $1 = KEY: real env wins, else the .env line (scripts/lib/env.sh)
  env_get "$ROOT/.env" "$1"
}

status() {
  local pids; pids="$(port_pids)"
  if [ -z "$pids" ]; then echo "align inspector DOWN (nothing on :$PORT)"; return 1; fi
  echo "align inspector UP on :$PORT (pids: $pids)"
  # probe + print the host the server is actually BOUND on: with NETRADIO_ALIGN_HOST
  # set to a LAN address nothing answers on loopback (the Mac app hit exactly this)
  local host; host="$(env_or_dotenv NETRADIO_ALIGN_HOST)"
  [ -z "$host" ] || [ "$host" = "0.0.0.0" ] && host=127.0.0.1
  echo "  http://$host:$PORT/align"
  [ "$host" != "127.0.0.1" ] && echo "  (bound to $host — clients, incl. the Mac app, must use this host, not 127.0.0.1)"
  # the DSP capability is the thing that actually bites (the venv + the originals)
  # — ask the running server rather than guessing from the env.
  # With Basic Auth configured the probe must SEND it (an unauthenticated probe
  # printed "DSP: unknown" against a perfectly healthy server); and a 401 back
  # still proves the server is UP — report that, not "unknown".
  local user pass out code body
  user="$(env_or_dotenv NETRADIO_AUTH_USER)"
  pass="$(env_or_dotenv NETRADIO_AUTH_PASS)"
  local curl_auth=()
  [ -n "$user" ] && [ -n "$pass" ] && curl_auth=(-u "$user:$pass")
  if out="$(curl -sS --max-time 3 ${curl_auth[@]+"${curl_auth[@]}"} \
              -w $'\n%{http_code}' \
              "http://$host:$PORT/api/align/sources" 2>/dev/null)"; then
    code="${out##*$'\n'}"; body="${out%$'\n'*}"
  else
    code=""; body=""
  fi
  case "$code" in
    200)
      case "$body" in
        *'"enabled": true'*|*'"enabled":true'*) echo "  DSP: enabled" ;;
        *) echo "  DSP: NOT CONFIGURED — build .venv (make venv) and point NETRADIO_SOURCES_DIR at the originals (else sources_local/)" ;;
      esac
      case "$body" in
        *'"streams": []'*|*'"streams":[]'*)
          echo "  DATA: no capture MP3s listed — set NETRADIO_MP3_DIR in .env to the capture MP3 directory" ;;
      esac
      case "$body" in
        *'"hints": []'*|*'"hints":[]'*)
          echo "  DATA: no hints sets loaded — see 'run_align.sh help' for the converter primer" ;;
      esac ;;
    401)
      echo "  UP (auth required) — the probe's Basic Auth was refused:" \
           "set/check NETRADIO_AUTH_USER/PASS in the env or .env" ;;
    *)
      echo "  DSP: unknown (no answer on /api/align/sources — still starting?)" ;;
  esac
}

# The checkout guard (scripts/lib/checkout.sh), before clear_port: a worktree usually has no .env,
# so its port is the default 8766, the LIVE align server's, and a worktree copy of this launcher
# would stop or replace it. A main checkout off `main` is not refused: the align server commits
# with plumbing and never moves the branch. NETRADIO_ALLOW_OFF_MAIN=1 opts out.
case "${1:-start}" in
  start|stop|restart)
    checkout_allowed_off_main || checkout_refuse_worktree "$ROOT" \
      "NETRADIO_READ_ONLY=1 NETRADIO_ALIGN_PORT=<another port> python3 scripts/align_server.py" ;;
esac

case "${1:-start}" in
  start|restart) launch; data_primer ;;
  stop)          clear_port; echo "stopped" ;;
  status)        status ;;
  running)       [ -n "$(port_pids)" ] ;;   # quiet predicate: 0 = listening on $PORT, 1 = not
  running-here)  checkout_listener_here "$ROOT" "$PORT" ;;   # quiet: 0 = the listener is THIS checkout's server
  help|-h|--help) echo "usage: $0 {start|stop|restart|status|running|running-here|help}"; data_primer ;;
  *) echo "usage: $0 {start|stop|restart|status|running|running-here|help}" >&2; exit 1 ;;
esac
