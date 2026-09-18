PYTHON=$(shell command -v python3)
ifeq (, $(PYTHON))
    $(error "PYTHON=$(PYTHON) not found in $(PATH)")
endif

# The ONE virtualenv is `.venv`, and uv builds and fills it (Tim, 2026-09-17; before that
# `python -m venv` + pip). `UV` resolves at parse time to the first uv that runs: the one on
# PATH, else the Homebrew binary. The fallback exists because an asdf shim can sit first on PATH
# and answer "No version is set for command uv" (exit 126) in any directory without a
# `.tool-versions`. Pass a path to skip the search: `make venv UV=/path/to/uv`. When neither
# runs, `uv-check` stops with the install hint.
ifndef UV
UV := $(firstword $(foreach c,uv /opt/homebrew/bin/uv,$(if $(shell $(c) --version 2>/dev/null),$(c))))
endif
# The interpreter uv builds `.venv` from. A version request: uv prefers an installed 3.13 and
# downloads one when none is installed, so there is always a fallback. 3.13 is the interpreter
# the harvester has run under since 2026-07 and the tested path. (The pin dates from librosa ->
# numba -> llvmlite having no 3.14 wheels; they do now.) A path works too:
# `make venv UV_PYTHON=/path/to/python3.14`.
UV_PYTHON ?= 3.13

# Machine-specific paths and credentials (NETRADIO_SOURCES_DIR, ...) live in `.env`, gitignored
# since this repo is PUBLIC. `VAR=value` lines, so the file is both make-includable and
# shell-sourceable (`set -a; . ./.env; set +a`). Optional: the leading `-` means "don't fail if
# it isn't there". Same name and format as the player repo's `.env`, so one block can serve both.
#
# Until 2026-09 `.env` was the general virtualenv DIRECTORY and the variables lived in
# `.env_vars`. `-include` of a directory stops make dead with "Is a directory", so say what
# happened instead. The old venv is not needed: `.venv` now carries every dependency.
ifneq (,$(wildcard .env/.))
    $(error ".env is the retired general virtualenv directory. Remove it -- rm -rf .env -- then: mv .env_vars .env  (or: cp .env.example .env)")
endif
# The other half of the same migration: the old directory is gone but the variables still sit in
# `.env_vars`. Ignoring that file silently would drop every path and credential it holds.
ifeq (,$(wildcard .env))
ifneq (,$(wildcard .env_vars))
    $(error ".env_vars is the old name of the variables file; it is no longer read. Rename it: mv .env_vars .env")
endif
endif
-include .env
export NETRADIO_SOURCES_DIR

#########################################
##### DEVELOPMENT ENVIRONMENT SETUP #####
#########################################
SHELL=bash

.DEFAULT_GOAL := env
env: venv match-tools   ## EVERYTHING the tools need, incl. the align binaries (sonic-annotator + match-vamp)

match-tools:          ## install/build sonic-annotator + the match-vamp plugin (idempotent; macOS)
	bash scripts/install_match_tools.sh

# One venv, every dependency: the label tooling (requirements.txt) AND the alignment engine +
# harvester (requirements-streamalign.txt: librosa, soundfile). Rebuilds from scratch. To add
# the label tooling to an existing `.venv` without rebuilding it: `make dep`.
# `make venv` never deletes an existing `.venv`: the harvester and the align server execute from
# it, and a bare `make` (default goal `env` -> `venv`) used to reach `rm -rf .venv` under them.
# Rebuilding from scratch is its own, deliberate verb: `make venv-rebuild`.
# A uv venv has no pip inside it; `uv pip` is the installer, so there is no `pip` target.
uv-check:             ## is there a uv that runs? prints which one
	@test -n "$(UV)" || { echo "uv not found: neither the uv on PATH nor /opt/homebrew/bin/uv runs. Install it: brew install uv   (or: make venv UV=/path/to/uv)"; exit 1; }
	@echo "uv: $(UV) ($$($(UV) --version))"

venv: uv-check        ## create .venv with BOTH requirement sets (refuses if it already exists)
	@test ! -e .venv/bin/python || { echo ".venv already built ($$(.venv/bin/python --version)). make dep installs into it; make venv-rebuild recreates it from scratch."; exit 1; }
	$(UV) venv .venv --python $(UV_PYTHON)
	$(UV) pip install --python .venv/bin/python -r requirements.txt -r requirements-streamalign.txt
	$(MAKE) scripts-pth
	@echo
	@echo "venv ready ($$(.venv/bin/python --version)). Activate it, then run every tool with python, e.g.:"
	@echo "  . .venv/bin/activate && python -m streamalign hints <stem>"
	@echo "(hints = prep for the file you are ABOUT to label; sort_tsv offers it for the next stem)"

venv-rebuild:         ## DELETE .venv and create it again (stop the harvester and the align server first)
	rm -rf .venv
	$(MAKE) venv

dep: uv-check         ## install/upgrade both requirement sets into the existing .venv (and refresh the .pth)
	@test -e .venv/bin/python || { echo ".venv is not built. Run: make venv"; exit 1; }
	$(UV) pip install --python .venv/bin/python --upgrade -r requirements.txt -r requirements-streamalign.txt
	$(MAKE) scripts-pth

# `. .venv/bin/activate && python -m streamalign ...` needs `scripts/` on sys.path, and there is
# no pyproject to install it from. So the venv carries `netradio-scripts.pth` in its site-packages
# with the absolute path of `scripts/` (Tim, 2026-09-17). It is written here, at make time, so no
# machine path is committed; `venv` and `dep` both run this, and a venv built by hand gets it from
# `make scripts-pth`. That is why no recipe below sets PYTHONPATH=scripts any more.
scripts-pth:          ## put <repo>/scripts on the venv's sys.path (site-packages/netradio-scripts.pth)
	@.venv/bin/python -c "import os, sys, sysconfig; p = os.path.join(sysconfig.get_paths()['purelib'], 'netradio-scripts.pth'); open(p, 'w').write(sys.argv[1] + chr(10)); print('scripts on sys.path:', p)" "$(CURDIR)/scripts"

# `dep` already upgrades within the pins. This one also freezes the result so the pins can be
# reviewed. It does not overwrite requirements.txt any more: that file is hand-maintained (the
# git source, the 3.13 marker, the comments), and a freeze lost all three.
dep-upgrade: dep      ## upgrade, then freeze the venv to requirements-latest.txt for review
	$(UV) pip freeze --python .venv/bin/python > requirements-latest.txt
	@echo "requirements-latest.txt is the frozen venv; requirements*.txt stay hand-maintained. Update the pins from it."

align-env: venv       ## alias, kept for muscle memory: the alignment engine now lives in the one venv

align-check:          ## verify the venv can do the librosa-backed work, and the originals resolve
	@.venv/bin/python -c "import librosa, numpy; print('librosa', librosa.__version__, '/ numpy', numpy.__version__)" \
	  || { echo "venv missing/incomplete — run: make venv"; exit 1; }
	@.venv/bin/python -c "import streamalign; print('streamalign from', streamalign.__path__[0])" \
	  || { echo "scripts/ is not on the venv's sys.path. Run: make scripts-pth"; exit 1; }
	@test -n "$(NETRADIO_SOURCES_DIR)" \
	  || { echo "NETRADIO_SOURCES_DIR unset — set it in .env (see .env.example)"; exit 1; }
	@test -d "$(NETRADIO_SOURCES_DIR)" \
	  && echo "originals OK: $(NETRADIO_SOURCES_DIR)" \
	  || { echo "NETRADIO_SOURCES_DIR does not resolve: $(NETRADIO_SOURCES_DIR)"; exit 1; }

# unittest, not pytest: pytest is in neither requirements file, and CI runs unittest too.
test:                 ## run the test suite
	.venv/bin/python -m unittest discover -s tests

# MallocLargeCache=0 tells macOS not to keep freed large blocks inside the process. Without it
# the harvester's footprint only ever goes up: it frees everything after each candidate, libmalloc
# holds the pages anyway, and under pressure they end up compressed and swapped. It has to be in
# the environment at process start, which is why it is here and not inside the script. Harmless on
# other platforms (an unknown variable). The fetch child sets it again for itself.
harvest-run:          ## work the queue (runs for weeks), with the memory bound in place
	set -a; [ -f .env ] && . ./.env; set +a; \
	MallocLargeCache=0 .venv/bin/python scripts/harvest.py --run

#########################################
#####          TRACKLIST            #####
#########################################
# This repo is CANONICAL for `track-metadata.json` (the player mirrors it). `make tracklist`
# enriches each linked track with artwork_url/full_page_url and renders the public TRACKLIST.md.
# `make sync` is the cross-repo sync — the SAME script runs in either repo (see scripts/
# tracklist_sync.sh): it moves track-metadata.json between repos via PRs (never commits to main),
# detects conflicts, and regenerates TRACKLIST.md. Cross-repo path from the env (no hardcoded
# paths): set NETRADIO_PLAYER_REPO. Pass ARGS=--dry-run to preview.

tracklist:            ## resolve artwork into track-metadata.json + render TRACKLIST.md (network)
	$(PYTHON) scripts/render_tracklist.py

# Both of these need NETRADIO_PLAYER_REPO, which is already in .env alongside every other
# machine path -- but make does not export what it -includes, so they failed with "set
# NETRADIO_PLAYER_REPO" even though it was set. Source it here. (`set -a` exports; `.env` is
# required for these targets anyway, but a missing file must not be a syntax error.)
sync:                 ## cross-repo tracklist sync (3-way, PR-based). Reads NETRADIO_PLAYER_REPO from .env. ARGS=--dry-run
	set -a; [ -f .env ] && . ./.env; set +a; \
	NETRADIO_ANALYSIS_REPO=$(CURDIR) bash scripts/tracklist_sync.sh $(ARGS)

tracklist-check:      ## report whether the analysis<->player track-metadata.json copies match
	set -a; [ -f .env ] && . ./.env; set +a; \
	NETRADIO_ANALYSIS_REPO=$(CURDIR) bash scripts/check_tracklist_sync.sh

env-check:            ## NETRADIO_* variables: set in .env but read by nothing (stale), or read but missing from .env.example. Exit 1 on either
	$(PYTHON) scripts/env_check.py
