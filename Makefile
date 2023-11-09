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
