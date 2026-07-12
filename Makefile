PYTHON=$(shell command -v python3)
ifeq (, $(PYTHON))
    $(error "PYTHON=$(PYTHON) not found in $(PATH)")
endif

# librosa -> numba -> llvmlite, which has no Python 3.14 wheels. The alignment venv is
# therefore pinned to 3.13, independently of whatever `python3` happens to be.
PYTHON313=$(shell command -v python3.13)

# Machine-specific paths (NETRADIO_SOURCES_DIR, ...). Gitignored, since this repo is PUBLIC.
# Written as `VAR=value` so it is both make-includable and shell-sourceable. Optional: the
# leading `-` means "don't fail if it isn't there".
-include .env_vars
export NETRADIO_SOURCES_DIR

#########################################
##### DEVELOPMENT ENVIRONMENT SETUP #####
#########################################
SHELL=bash

.DEFAULT_GOAL := env
env: venv dep

venv:
	rm -rf .env
	$(PYTHON) -m venv .env
	.env/bin/pip install --upgrade pip

dep-upgrade:
	pip-review --auto
	.env/bin/pip freeze -r requirements.txt | grep -B100 "pip freeze" | grep -v "pip freeze" > requirements-latest.txt
	rm requirements.txt
	mv requirements-latest.txt requirements.txt

dep: pip
	PIP_CONFIG_FILE=./env/pip.conf .env/bin/pip install -r requirements.txt --upgrade

pip:
	.env/bin/pip install --upgrade pip

#########################################
#####   ALIGNMENT ENGINE VENV (.venv) ###
#########################################
# The core engine (groundtruth/align/skips/solve) needs only numpy + ffmpeg and runs under
# the general `.env` venv. But the original-track <-> mix alignment (track_mix's chroma+DTW,
# and the origNNN spans in `streamalign hints`) needs librosa, whose numba/llvmlite chain has
# no Python 3.14 wheels. So librosa lives in a SEPARATE, 3.13-pinned venv: `.venv`.
#
# Two venvs, deliberately: `.env` tracks whatever python3 is current; `.venv` stays on 3.13
# for as long as numba needs it. Run the librosa-backed tools with .venv/bin/python.

align-env:            ## create .venv (python3.13) + install the alignment engine deps (librosa)
ifeq (, $(PYTHON313))
	$(error "python3.13 not found — librosa's numba/llvmlite have no 3.14 wheels. brew install python@3.13")
endif
	rm -rf .venv
	$(PYTHON313) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-streamalign.txt
	@echo
	@echo "alignment venv ready. Run the librosa-backed tools with .venv/bin/python, e.g.:"
	@echo "  PYTHONPATH=scripts .venv/bin/python -m streamalign hints d356-375"

align-check:          ## verify the alignment venv can do the librosa-backed work
	@.venv/bin/python -c "import librosa, numpy; print('librosa', librosa.__version__, '/ numpy', numpy.__version__)" \
	  || { echo "alignment venv missing/incomplete — run: make align-env"; exit 1; }
	@test -n "$(NETRADIO_SOURCES_DIR)" \
	  || { echo "NETRADIO_SOURCES_DIR unset — set it in .env_vars (see .env_vars.example)"; exit 1; }
	@test -d "$(NETRADIO_SOURCES_DIR)" \
	  && echo "originals OK: $(NETRADIO_SOURCES_DIR)" \
	  || { echo "NETRADIO_SOURCES_DIR does not resolve: $(NETRADIO_SOURCES_DIR)"; exit 1; }

test:                 ## run the test suite
	.env/bin/python -m pytest tests/ -q

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

sync:                 ## cross-repo tracklist sync (3-way, PR-based). Needs NETRADIO_PLAYER_REPO. ARGS=--dry-run
	NETRADIO_ANALYSIS_REPO=$(CURDIR) bash scripts/tracklist_sync.sh $(ARGS)

tracklist-check:      ## report whether the analysis<->player track-metadata.json copies match
	NETRADIO_ANALYSIS_REPO=$(CURDIR) bash scripts/check_tracklist_sync.sh
