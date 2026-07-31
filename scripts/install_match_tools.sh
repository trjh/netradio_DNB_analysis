#!/bin/bash
# Install the align-tool binaries: sonic-annotator + the match-vamp Vamp plugin.
#
# Neither ships usable macOS binaries for our purpose: sonic-annotator has a GitHub
# release (universal binary), match-vamp must be BUILT from source (no releases; the
# soundsoftware.ac.uk download site is dead). Idempotent: each piece is skipped when
# already present. Apple Silicon and Intel both work (the build uses the host arch).
set -euo pipefail

SA_VERSION="1.7.0"
SA_URL="https://github.com/sonic-visualiser/sonic-annotator/releases/download/sonic-annotator-1.7/sonic-annotator-${SA_VERSION}-macos.tar.gz"
BIN_DIR="${BIN_DIR:-$(brew --prefix)/bin}"
VAMP_DIR="$HOME/Library/Audio/Plug-Ins/Vamp"
BUILD_DIR="${TMPDIR:-/tmp}/match-tools-build"

echo "== sonic-annotator =="
if command -v sonic-annotator >/dev/null 2>&1; then
    echo "already installed: $(command -v sonic-annotator) ($(sonic-annotator -v 2>&1))"
else
    mkdir -p "$BUILD_DIR"
    curl -sL "$SA_URL" | tar xz -C "$BUILD_DIR"
    install -m 0755 "$BUILD_DIR/sonic-annotator-${SA_VERSION}-macos/sonic-annotator" "$BIN_DIR/"
    echo "installed $BIN_DIR/sonic-annotator ($(sonic-annotator -v 2>&1))"
fi

echo "== match-vamp plugin =="
if [ -f "$VAMP_DIR/match-vamp-plugin.dylib" ]; then
    echo "already installed: $VAMP_DIR/match-vamp-plugin.dylib"
else
    if ! brew list vamp-plugin-sdk >/dev/null 2>&1; then
        brew install vamp-plugin-sdk
    fi
    SDK="$(brew --prefix vamp-plugin-sdk)"
    ARCH="$(uname -m)"
    mkdir -p "$BUILD_DIR"
    [ -d "$BUILD_DIR/match-vamp" ] || git clone -q --depth 1 https://github.com/c4dm/match-vamp "$BUILD_DIR/match-vamp"
    make -C "$BUILD_DIR/match-vamp" -f Makefile.osx plugin \
        ARCHFLAGS="-arch $ARCH" \
        CFLAGS="-arch $ARCH -O3 -I$SDK/include -Wall -fPIC -std=c++11" \
        CXXFLAGS="-arch $ARCH -O3 -I$SDK/include -Isrc -DUSE_COMPACT_TYPES -Wall -fPIC -std=c++11" \
        LDFLAGS="-L$SDK/lib -lvamp-sdk -arch $ARCH" >/dev/null
    mkdir -p "$VAMP_DIR"
    install -m 0644 "$BUILD_DIR/match-vamp/match-vamp-plugin.dylib" \
                    "$BUILD_DIR/match-vamp/match-vamp-plugin.cat" \
                    "$BUILD_DIR/match-vamp/match-vamp-plugin.n3" "$VAMP_DIR/"
    echo "built + installed $VAMP_DIR/match-vamp-plugin.dylib ($ARCH)"
fi

echo "== verify =="
sonic-annotator -l 2>/dev/null | grep -m1 "match-vamp-plugin:match" \
    && echo "OK: MATCH transform visible" \
    || { echo "FAIL: sonic-annotator cannot see the MATCH plugin"; exit 1; }
