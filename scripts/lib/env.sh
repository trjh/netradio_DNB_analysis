# shellcheck shell=bash
# scripts/lib/env.sh — the one `.env` reader the shell scripts share. Source it; it defines
# functions and sets nothing.
#
#   . "$ROOT/scripts/lib/env.sh"
#   env_value FILE KEY    # KEY's value in FILE, or nothing
#   env_has FILE KEY      # does FILE hold a line for KEY (an empty value included)
#   env_get FILE KEY      # the real environment's KEY when it is set, else env_value FILE KEY
#   env_load FILE         # export every key in FILE that the environment does not already hold
#   env_home PATH         # a leading `~` becomes $HOME, as Python's expanduser does
#
# It reads a `.env` exactly as envfile, the one Python reader, does (tests/test_envfile.py runs both
# on the same files), and never executes it:
#   - each line is trimmed; a blank line, a line starting with `#`, and a line with no `=` are
#     skipped
#   - the line splits on its first `=`, and both sides are trimmed
#   - double quotes, then single quotes, are stripped from both ends of the value
#   - the first line for a key wins (the loaders use setdefault)
#   - `export KEY=v` is a key named "export KEY", which nothing reads
#   - a `#` after a value is part of the value: a comment goes on a line of its own
#   - nothing is expanded: no `$VAR`, no command substitution. A leading `~` stays as written, for
#     the reader to expand, as the Python side does with expanduser: a script that builds a path
#     from a value calls env_home on it first.
# A missing FILE reads as empty.

# _env_pairs FILE — each line's key and value, in file order, as two lines (a value read from one
# line cannot hold a newline).
_env_pairs() {
  [ -f "$1" ] || return 0
  awk '{
    line = $0; gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
    if (line == "" || substr(line, 1, 1) == "#") next
    i = index(line, "="); if (!i) next
    key = substr(line, 1, i - 1); gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
    v = substr(line, i + 1); gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
    gsub(/^"+|"+$/, "", v); gsub(/^\047+|\047+$/, "", v)
    print key; print v
  }' "$1"
}

env_value() {      # $1 = file, $2 = KEY
  local key val
  while IFS= read -r key && IFS= read -r val; do
    if [ "$key" = "$2" ]; then printf '%s' "$val"; return 0; fi
  done < <(_env_pairs "$1")
  return 0
}

env_has() {        # $1 = file, $2 = KEY
  local key val
  while IFS= read -r key && IFS= read -r val; do
    [ "$key" = "$2" ] && return 0
  done < <(_env_pairs "$1")
  return 1
}

_env_name_ok() { [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; }

env_get() {        # $1 = file, $2 = KEY (a shell name)
  if [ -n "${!2+x}" ]; then printf '%s' "${!2}"; else env_value "$1" "$2"; fi
}

env_load() {       # $1 = file
  local key val
  while IFS= read -r key && IFS= read -r val; do
    _env_name_ok "$key" || continue     # not a name a shell can export; nothing reads it
    [ -n "${!key+x}" ] && continue      # set already: the real environment, or an earlier line
    export "$key=$val"
  done < <(_env_pairs "$1")
  return 0
}

env_home() {       # $1 = a path: a leading `~` alone or `~/` becomes $HOME; nothing else changes
  # The patterns match the literal text a .env line holds; nothing is meant to expand in them.
  # shellcheck disable=SC2088
  case "$1" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}
