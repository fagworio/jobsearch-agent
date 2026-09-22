.PHONY: test compile test-core-runtime test-browser-runtime test-linkedin-runtime test-submission-runtime help

help:
	@echo "make test                       - run pytest"
	@echo "make compile                   - compile Python sources"
	@echo "make test-core-runtime        - run provider-neutral tests"
	@echo "make test-browser-runtime     - run Playwright fixture tests"
	@echo "make test-linkedin-runtime    - run local LinkedIn fixture tests"
	@echo "make test-submission-runtime  - run local submission-server tests"

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
	python3 -m pytest -q tests/test_playwright_runtime.py

test-linkedin-runtime:
	python3 -m pytest -q tests/test_linkedin_inspector.py tests/test_linkedin_qa.py tests/test_linkedin_session.py

test-submission-runtime:
	python3 -m pytest -q tests/test_greenhouse_submission.py tests/test_submission_boundary.py

compile:
	python3 -m compileall -q src
