# shellcheck shell=bash
# Sourced by the hooks here. chain_global NAME [ARGS...] runs the same-named hook from the
# global core.hooksPath (read from the user's git config, ~ expanded) when it exists and is
# executable, passing the arguments and stdin through, and returns its exit status.
chain_global() {
  local name="$1" dir hook; shift
  dir="$(git config --global --includes --get core.hooksPath 2>/dev/null)" || return 0
  case "$dir" in "~"*) dir="$HOME${dir#"~"}" ;; esac
  hook="$dir/$name"
  [ -n "$dir" ] && [ -x "$hook" ] || return 0
  [ "$(cd "$dir" && pwd -P)" = "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)" ] && return 0
  "$hook" "$@"
}
