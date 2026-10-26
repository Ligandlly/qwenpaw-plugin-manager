PY ?= python3
VENV ?= .venv

.PHONY: help venv test lint clean

help:
	@echo "make venv   create .venv and install the package with dev extras"
	@echo "make test   run the test suite"
	@echo "make clean  remove caches and build artefacts"

venv:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/python -m pip install -e '.[dev]'

test:
	$(VENV)/bin/python -m pytest

lint:
	$(VENV)/bin/python -m compileall -q qpm tests

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
