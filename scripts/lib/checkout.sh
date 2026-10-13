# shellcheck shell=bash
# scripts/lib/checkout.sh — the checkout guard and the listener check the launchers share. Source it; it defines functions
# and sets nothing.
#
#   . "$ROOT/scripts/lib/checkout.sh"
#   checkout_allowed_off_main         # exit 0 when NETRADIO_ALLOW_OFF_MAIN is exactly "1"
#   checkout_refuse_worktree ROOT HINT
#                                     # exit 2 (the script, not the function) when ROOT is a linked
#                                     # worktree; HINT is the line that says how to run by hand
#   checkout_refuse_off_main ROOT [HINT]
#                                     # exit 2 when ROOT's HEAD is not refs/heads/main; HINT is the
#                                     # line that says how to start anyway (default: this launcher's
#                                     # own `start`, with the opt-out set)
#   checkout_listener_here ROOT PORT  # exit 0 when a listener on PORT has ROOT as its working
#                                     # directory, 1 when none does (or nothing listens), 2 when
#                                     # a listener's working directory cannot be read
#
# The two refusals run before a launcher takes a lock or touches a port, so a refusal changes nothing.
# Only the explicit "1" in NETRADIO_ALLOW_OFF_MAIN opts out of either; a misconfigured value never
# disarms the guard.

checkout_allowed_off_main() {
  [ "${NETRADIO_ALLOW_OFF_MAIN:-}" = "1" ]
}

# A linked worktree has a `.git` FILE (`gitdir: ...`); the root checkout has a directory. Read from
# the filesystem, no git needed. Anything at `.git` that is not a directory (a broken link) counts.
checkout_is_worktree() {
  { [ -e "$1/.git" ] || [ -L "$1/.git" ]; } && [ ! -d "$1/.git" ]
}

checkout_refuse_worktree() {
  checkout_is_worktree "$1" || return 0
  echo "REFUSING: $1 is a linked worktree, and this launcher manages the live port." >&2
  echo "Start a worktree server by hand: $2" >&2
  exit 2
}

# The branch name, "HEAD" for a detached HEAD, or nothing (status 1) when git cannot answer.
# `symbolic-ref -q` exits 1 for a detached HEAD and 128 (or 127, git missing) for a real failure,
# and names the ref itself, so a tag called `main` cannot make the answer ambiguous.
checkout_branch() {
  local ref rc=0
  ref="$(git -C "$1" symbolic-ref -q HEAD 2>/dev/null)" || rc=$?
  case "$rc" in
    0) printf '%s\n' "${ref#refs/heads/}" ;;
    1) echo HEAD ;;
    *) return 1 ;;
  esac
}

# A branch git cannot read is not refused: the guard never stops the live server because it could
# not ask.
checkout_refuse_off_main() {
  local b
  b="$(checkout_branch "$1")" || return 0
  [ -z "$b" ] || [ "$b" = "main" ] && return 0
  echo "REFUSING: $1 is on branch '$b', not main. The live server runs from the repo" >&2
  echo "root, which stays on main. Off main on purpose?" >&2
  echo "  ${2:-NETRADIO_ALLOW_OFF_MAIN=1 $0 start}" >&2
  exit 2
}

# Is the listener on PORT a server started from ROOT? A launcher starts its server with its checkout
# as the working directory, so a listening pid whose cwd resolves to ROOT is that checkout's; one
# started from another checkout on the same port is not. lsof gives both the pids and the cwd.
checkout_listener_here() {
  local here pid cwd resolved unknown=0
  here="$(cd "$1" && pwd -P)" || return 2
  for pid in $(lsof -nP -tiTCP:"$2" -sTCP:LISTEN 2>/dev/null || true); do
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true)"
    resolved=""
    if [ -n "$cwd" ]; then resolved="$(cd "$cwd" 2>/dev/null && pwd -P)" || resolved=""; fi
    if [ -z "$resolved" ]; then unknown=1; continue; fi
    [ "$resolved" = "$here" ] && return 0
  done
  return $((1 + unknown))
}
