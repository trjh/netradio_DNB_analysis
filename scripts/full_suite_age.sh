#!/usr/bin/env bash
#
# How long since the FULL suite last passed: `make test-container`, the run with ffmpeg that CI
# skips. That target records each pass in the git directory all checkouts share
# (<git-common-dir>/full-suite-run: epoch seconds, UTC time, commit), so a run from any worktree
# counts, and nothing is committed.
#
#   scripts/full_suite_age.sh           one line: when, which commit, how far main has moved since
#   scripts/full_suite_age.sh --record  write the record for HEAD (the Makefile calls this)
#
# The line starts with OVERDUE once the last pass is older than FULL_SUITE_MAX_DAYS (default 14)
# or there is no record. It always exits 0, so a caller can print it anywhere.

set -u
common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
  echo "full suite: not in a git checkout"; exit 0; }
stamp="$common/full-suite-run"

if [ "${1:-}" = "--record" ]; then
  printf '%s %s %s\n' "$(date +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(git rev-parse HEAD)" >|"$stamp"
  exit 0
fi

max_days="${FULL_SUITE_MAX_DAYS:-14}"
if [ ! -f "$stamp" ]; then
  echo "OVERDUE: the full suite has no recorded pass here. Run: make test-container"
  exit 0
fi
read -r epoch when sha <"$stamp"
days=$(( ($(date +%s) - epoch) / 86400 ))
since="unknown"
if git cat-file -e "$sha^{commit}" 2>/dev/null && git rev-parse -q --verify origin/main >/dev/null; then
  since="$(git rev-list --count "$sha..origin/main") commit(s) on origin/main since"
fi
line="full suite (make test-container) last passed ${when%%T*}, $days day(s) ago, at ${sha:0:7}; $since"
if [ "$days" -ge "$max_days" ]; then
  echo "OVERDUE (over $max_days days): $line. Run: make test-container"
else
  echo "$line"
fi
