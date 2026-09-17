PYTHON=$(shell command -v python3)
ifeq (, $(PYTHON))
    $(error "PYTHON=$(PYTHON) not found in $(PATH)")
endif

# The ONE virtualenv is `.venv`. It prefers python3.13 when that is installed -- the interpreter
# the harvester has run under since 2026-07 -- and falls back to whatever `python3` is. (The
# 3.13 pin dates from librosa -> numba -> llvmlite having no 3.14 wheels; they do now, so the
# fallback works, but the tested path is still 3.13.)
VENV_PYTHON=$(or $(shell command -v python3.13),$(PYTHON))

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
venv:                 ## (re)create .venv with BOTH requirement sets
	rm -rf .venv
	$(VENV_PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt -r requirements-streamalign.txt
	@echo
	@echo "venv ready ($$(.venv/bin/python --version)). Run every tool with .venv/bin/python, e.g.:"
	@echo "  PYTHONPATH=scripts .venv/bin/python -m streamalign hints <stem>"
	@echo "(hints = prep for the file you are ABOUT to label; sort_tsv offers it for the next stem)"

dep: pip              ## install/upgrade both requirement sets into the existing .venv
	.venv/bin/pip install -r requirements.txt -r requirements-streamalign.txt --upgrade

pip:
	.venv/bin/pip install --upgrade pip

dep-upgrade:
	pip-review --auto
	.venv/bin/pip freeze -r requirements.txt | grep -B100 "pip freeze" | grep -v "pip freeze" > requirements-latest.txt
	rm requirements.txt
	mv requirements-latest.txt requirements.txt

align-env: venv       ## alias, kept for muscle memory: the alignment engine now lives in the one venv

align-check:          ## verify the venv can do the librosa-backed work, and the originals resolve
	@.venv/bin/python -c "import librosa, numpy; print('librosa', librosa.__version__, '/ numpy', numpy.__version__)" \
	  || { echo "venv missing/incomplete — run: make venv"; exit 1; }
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
	MallocLargeCache=0 PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --run

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
