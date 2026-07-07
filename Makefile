VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: setup test doctor auth backup verify dedup reorganize purge

setup:
	python3 -m venv $(VENV)
	$(PIP) install -q -U pip
	$(PIP) install -q -r requirements.txt

test:
	$(PY) -m pytest

doctor:
	$(PY) -m gdrivefilter doctor

auth:
	$(PY) -m gdrivefilter auth --account $(ACCOUNT)

# READ-ONLY. Preflight will stop and ask for a hard drive if space is short.
backup:
	$(PY) -m gdrivefilter backup --account $(ACCOUNT)

verify:
	$(PY) -m gdrivefilter verify --dir $(DIR)

dedup:
	$(PY) -m gdrivefilter dedup --dir $(DIR) $(if $(SEMANTIC),--semantic,)

reorganize:
	$(PY) -m gdrivefilter reorganize --dir $(DIR) --dest $(DEST)

# Guarded: needs verified primary + external backup and the explicit flag.
purge:
	$(PY) -m gdrivefilter purge --primary $(DIR) --dest $(DEST) --i-have-a-verified-backup $(if $(APPLY),--apply,)

ACCOUNT ?= default
