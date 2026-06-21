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
#####   TRACKLIST PUBLISHING (public)   #####
#########################################
# This repo is CANONICAL for `track-metadata.json`; the player mirrors it. These targets
# regenerate the public TRACKLIST.md and publish changes via a branch + PR you accept on the
# CLI. Cross-repo path comes from the env (no hardcoded paths): set NETRADIO_PLAYER_REPO.
TRACKLIST_BRANCH ?= tracklist-update-$(shell date +%Y%m%d-%H%M%S)

tracklist:            ## regenerate TRACKLIST.md (+ artwork cache) from track-metadata.json
	$(PYTHON) scripts/render_tracklist.py

tracklist-check:      ## verify the analysis<->player track-metadata.json copies match
	NETRADIO_ANALYSIS_REPO=$(CURDIR) bash scripts/check_tracklist_sync.sh

tracklist-pr: tracklist   ## branch with the tracklist JSON + render, and commit it
	git checkout -b $(TRACKLIST_BRANCH)
	git add track-metadata.json TRACKLIST.md tracklist_artwork.json
	git commit -m "tracklist: refresh track-metadata.json + TRACKLIST.md"
	@echo "branch $(TRACKLIST_BRANCH) ready — run 'make push' to publish + PR."

push:                 ## push the current branch, open a PR, then offer to accept it on the CLI
	git push -u origin $$(git branch --show-current)
	gh pr create --fill --base main || echo "(PR may already exist)"
	@read -r -p "Accept (merge) this PR now? [y/N] " a; \
	  if [ "$$a" = "y" ] || [ "$$a" = "Y" ]; then \
	    gh pr merge --merge --delete-branch && echo "merged ✓"; \
	  else echo "left open for review."; fi
