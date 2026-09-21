.PHONY: test compile help

help:
	@echo "make test     - run pytest"
	@echo "make compile  - compile Python sources"

test:
	python3 -m pytest

compile:
	python3 -m compileall -q src

