#!/bin/bash
# scripts/post_pull_restart.sh -- restart the align server when a pull changed the code it runs.
# Called by .githooks/post-merge, which git runs after every merge or fast-forward: a `git pull`
# fires it. Switched on once per machine by `make hooks`.
#
# Why: an align server left running on old code while the checkout moves to new code serves
# stale behaviour, and its review-sync thread commits through code the checkout no longer has.
#
# Does nothing in a linked worktree (hooks are shared across worktrees) or off `main`. Only a
# change under scripts/ or public/ (the code the server and its DSP child load, and the pages it
# serves) restarts it; a pull of labels, data or documentation alone says so and does nothing.
# A server that is down is left down.
#
# NETRADIO_PULL_RESTART=0 skips it (read from the environment, not .env). Output goes to stderr;
# never interactive. A failed restart exits non-zero (git ignores the status of post-merge, so
# the message is what counts).
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/checkout.sh
. "$ROOT/scripts/lib/checkout.sh"

say() { echo "post-pull restart: $*" >&2; }

if [ "${NETRADIO_PULL_RESTART:-}" = "0" ]; then
  say "skipped (NETRADIO_PULL_RESTART=0). Restart by hand: scripts/run_align.sh restart"
  exit 0
fi
checkout_is_worktree "$ROOT" && exit 0
[ "$(checkout_branch "$ROOT" 2>/dev/null)" = "main" ] || exit 0

# What moved: ORIG_HEAD..HEAD (git sets ORIG_HEAD for a pull's merge or fast-forward), else the
# previous reflog position of HEAD. If neither can be read, assume code changed.
changed="" ok=0
for base in ORIG_HEAD 'HEAD@{1}'; do
  if changed="$(git -C "$ROOT" diff --name-only "$base" HEAD 2>/dev/null)"; then ok=1; break; fi
done
if [ "$ok" = 1 ]; then
  code=0
  while IFS= read -r path; do
    case "$path" in
      scripts/* | public/*) code=1; break ;;
    esac
  done <<EOF2
$changed
EOF2
  if [ "$code" = 0 ]; then
    say "no change under scripts/ or public/; nothing to restart."
    exit 0
  fi
else
  say "could not tell what changed; treating it as a code change."
fi

# A server started from here must not inherit git's hook environment (git-core first on PATH,
# GIT_EXEC_PATH, GIT_EDITOR, GIT_REFLOG_ACTION, GITHEAD_* ...).
for v in $(compgen -e | grep '^GIT'); do unset "$v"; done
gitcore="$(git --exec-path 2>/dev/null)"
if [ -n "$gitcore" ]; then
  newpath=""
  oldifs="$IFS"; IFS=:
  for d in $PATH; do [ "$d" = "$gitcore" ] || newpath="${newpath:+$newpath:}$d"; done
  IFS="$oldifs"
  PATH="$newpath"
fi

script="$ROOT/scripts/run_align.sh"
[ -x "$script" ] || exit 0
"$script" running >/dev/null 2>&1 || exit 0
say "pull changed code; restarting the align server (it was running)."
if ! "$script" restart >&2; then
  say "FAILED: the align server did not restart. Run scripts/run_align.sh restart and read its output."
  exit 1
fi
exit 0
