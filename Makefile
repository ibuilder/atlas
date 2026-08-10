# Atlas PMOS developer commands.
# SPDX-License-Identifier: MIT

.DEFAULT_GOAL := help
.PHONY: help setup demo run test lint format typecheck audit check migrate upgrade downgrade seed shell docker clean

PY := python
VENV := .venv
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
else
BIN := $(VENV)/bin
endif
FLASK := $(BIN)/flask --app wsgi

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv, install dependencies, install hooks
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip setuptools wheel
	$(BIN)/python -m pip install -e ".[dev,postgres]"
	-$(BIN)/pre-commit install
	@echo "Setup complete. Copy .env.example to .env, then run: make demo"

demo: ## Migrate, seed a full demo portfolio, and run the app
	$(FLASK) db upgrade || $(BIN)/alembic upgrade head
	$(FLASK) seed demo
	$(FLASK) run --debug

run: ## Run the development server
	$(FLASK) run --debug

test: ## Run the test suite with coverage
	$(BIN)/python -m pytest --cov=app --cov-report=term-missing --cov-report=xml

test-fast: ## Run the test suite without coverage
	$(BIN)/python -m pytest -q

lint: ## Check formatting and lint rules
	$(BIN)/python -m ruff check app tests migrations
	$(BIN)/python -m black --check app tests wsgi.py

format: ## Apply formatting and safe lint fixes
	$(BIN)/python -m ruff check app tests migrations --fix
	$(BIN)/python -m black app tests wsgi.py

typecheck: ## Static type analysis
	$(BIN)/python -m mypy app

audit: ## Security scan of code and dependencies
	$(BIN)/python -m bandit -r app -q -c pyproject.toml
	$(BIN)/python -m pip_audit --strict || true

check: lint typecheck audit test ## Everything CI runs

migrate: ## Generate a migration: make migrate m="add widget table"
	$(BIN)/alembic revision --autogenerate -m "$(m)"

upgrade: ## Apply migrations
	$(BIN)/alembic upgrade head

downgrade: ## Roll back one migration
	$(BIN)/alembic downgrade -1

seed: ## Seed the demo organization
	$(FLASK) seed demo

verify-audit: ## Verify the demo organization's audit chain
	$(FLASK) atlas verify-audit --org northlight

check-schema: ## Verify tenancy and schema invariants
	$(FLASK) atlas check-schema

shell: ## Flask shell with every model in scope
	$(FLASK) shell

openapi: ## Write the OpenAPI document to openapi.json
	$(BIN)/python -c "from app import create_app; from app.api.openapi import build_spec; import json; app=create_app(); ctx=app.test_request_context(); ctx.push(); print(json.dumps(build_spec(), indent=2))" > openapi.json
	@echo "Wrote openapi.json"

docker: ## Build and run the full stack
	docker compose up --build

clean: ## Remove caches and build artefacts
	-rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml build dist
	-find . -type d -name __pycache__ -prune -exec rm -rf {} +
