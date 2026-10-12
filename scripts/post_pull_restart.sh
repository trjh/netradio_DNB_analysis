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
# A server that is down is left down, and so is a server on the port that another checkout
# started, or one whose checkout cannot be told (`run_align.sh running-here` exits 1 or 2): this
# hook restarts only its own checkout's server.
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

# A server started from here must not inherit what git puts into a hook's environment: git-core
# first on PATH, and the variables below. The user's own GIT_AUTHOR_*, GIT_COMMITTER_*,
# GIT_SSH_COMMAND and the like stay, so the server's review sync commits and pushes as it would
# after a restart by hand. The git-core directory is read BEFORE the unset: inside the hook,
# GIT_EXEC_PATH is the one git is actually running from.
gitcore="${GIT_EXEC_PATH:-$(git --exec-path 2>/dev/null)}"
for v in GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_EXEC_PATH GIT_PREFIX GIT_REFLOG_ACTION \
         GIT_EDITOR GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_QUARANTINE_PATH \
         GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
         $(compgen -e | grep -E '^(GITHEAD_|GIT_CONFIG_KEY_|GIT_CONFIG_VALUE_)'); do
  unset "$v"
done
if [ -n "$gitcore" ]; then
  newpath=""
  oldifs="$IFS"; IFS=:
  for d in $PATH; do [ "$d" = "$gitcore" ] || newpath="${newpath:+$newpath:}$d"; done
  IFS="$oldifs"
  PATH="$newpath"
fi

script="$ROOT/scripts/run_align.sh"
[ -x "$script" ] || exit 0
"$script" running-here >/dev/null 2>&1; here=$?
if [ "$here" != 0 ]; then
  if "$script" running >/dev/null 2>&1; then
    if [ "$here" = 2 ]; then
      say "could not tell whether the align server on its port is this checkout's; left alone."
    else
      say "the align server on its port belongs to another checkout; left alone."
    fi
  fi
  exit 0
fi
say "pull changed code; restarting the align server (it was running)."
if ! "$script" restart >&2; then
  say "FAILED: the align server did not restart. Run scripts/run_align.sh restart and read its output."
  exit 1
fi
exit 0
