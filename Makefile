PYTHON=$(shell command -v python3)
ifeq (, $(PYTHON))
    $(error "PYTHON=$(PYTHON) not found in $(PATH)")
endif

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
