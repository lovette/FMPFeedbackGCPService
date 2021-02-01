.PHONY: \
	help \
	pip-sync \
	pyclean \
	requirements \
	rmvirtualenv \
	virtualenv

VENVNAME := $(shell basename $(CURDIR))

ERROR_NO_VIRTUALENV = $(error Python virtualenv is not active, activate first)
ERROR_ACTIVE_VIRTUALENV = $(error Python virtualenv is active, deactivate first)

help:
	@echo 'Usage:'
	@echo '   make virtualenv     Create virtual environment'
	@echo '   make rmvirtualenv   Remove virtual environment'
	@echo '   make requirements   Generate requirements.txt files'
	@echo '   make pip-sync       Install only production packages'
	@echo '   make pyclean        Remove Python __pycache__ directories'

############################
# Requirements.txt

REQUIREMENTS_IN=requirements.in \
	cloudfunctions/fmpfeedback_caretaker/requirements.in \
	cloudfunctions/fmpfeedback_comment/requirements.in \
	cloudfunctions/fmpfeedback_mailgun/requirements.in \
	cloudfunctions/fmpfeedback_upload/requirements.in

REQUIREMENTS_TXT=$(REQUIREMENTS_IN:.in=.txt)

%.txt: %.in
	pip-compile -o $@ --quiet $<

requirements.txt: $(REQUIREMENTS_IN)
ifndef VIRTUAL_ENV
	$(ERROR_NO_VIRTUALENV)
endif
	pip-compile -o requirements.txt --quiet $(REQUIREMENTS_IN)

requirements:  $(REQUIREMENTS_TXT)

############################
# Virtualenv

virtualenv:
ifdef VIRTUAL_ENV
	$(ERROR_ACTIVE_VIRTUALENV)
endif
	python3 -m venv --prompt ${VENVNAME} .venv
	.venv/bin/python3 -m pip install --upgrade pip
	.venv/bin/pip3 install pip-tools
	@echo "EMPTY Python virtualenv named '${VENVNAME}' created in .venv directory"
	@echo "To activate: source .venv/bin/activate"
	@echo "To install packages: 'make pip-sync'"

rmvirtualenv: pyclean
ifdef VIRTUAL_ENV
	$(ERROR_ACTIVE_VIRTUALENV)
endif
	rm -rf .venv

############################
# pip-sync

pip-sync: requirements.txt
ifndef VIRTUAL_ENV
	$(ERROR_NO_VIRTUALENV)
endif
	pip-sync

############################
# Convenience

pyclean:
	find . -type d -name __pycache__ -exec rm -rf {} \+
