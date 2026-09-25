.PHONY: test compile test-core-runtime test-browser-runtime test-linkedin-runtime test-submission-runtime test-invariants test-architecture help

help:
	@echo "make test                       - run pytest"
	@echo "make compile                   - compile Python sources"
	@echo "make test-core-runtime        - run provider-neutral tests"
	@echo "make test-browser-runtime     - run Playwright fixture tests"
	@echo "make test-linkedin-runtime    - run local LinkedIn fixture tests"
	@echo "make test-submission-runtime  - run local submission-server tests"
	@echo "make test-invariants          - run the challenge/submit integration invariants"
	@echo "make test-architecture        - run the four architecture rings (lint-imports, AST, mypy, runtime)"

test:
	python3 -m pytest

test-core-runtime:
	python3 -m pytest -q tests \
		--ignore=tests/test_playwright_runtime.py \
		--ignore=tests/test_greenhouse_submission.py \
		--ignore=tests/test_submission_boundary.py \
		--ignore=tests/test_linkedin_inspector.py \
		--ignore=tests/test_linkedin_qa.py \
		--ignore=tests/test_linkedin_session.py \
		-m "not requires_libreoffice"

test-browser-runtime:
	python3 -m pytest -q tests/test_playwright_runtime.py tests/e2e/

# Invariantes entre o loop real e o challenge-guard real: exige Chromium, nao
# exige LibreOffice (o curriculo do cenario e um PDF em branco).
test-invariants:
	@python3 -c "import playwright.sync_api" 2>/dev/null || { \
		echo "Chromium/Playwright ausente no interpretador ativo."; \
		echo "Use o venv: .venv/bin/python -m pytest tests/integration -m integration"; \
		exit 1; }
	python3 -m pytest -q tests/integration/ -m integration

test-linkedin-runtime:
	python3 -m pytest -q tests/test_linkedin_inspector.py tests/test_linkedin_qa.py tests/test_linkedin_session.py

test-submission-runtime:
	python3 -m pytest -q tests/test_greenhouse_submission.py tests/test_submission_boundary.py

compile:
	python3 -m compileall -q src

# Os quatro aneis de contencao. Cada um pega uma classe diferente de violacao:
# dependencias (import-linter), nomes/campos/tipos (AST), anotacoes (mypy),
# imports dinamicos (runtime) — e o selfcheck garante que nao sao vacuos.
test-architecture:
	python3 -m compileall -q src
	$$(dirname $$(which python3))/lint-imports --no-cache
	$$(dirname $$(which python3))/mypy
	python3 -m pytest -q tests/test_architecture_boundaries.py tests/test_runtime_module_isolation.py tests/test_contracts_selfcheck.py
