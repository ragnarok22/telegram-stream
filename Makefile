.DEFAULT_GOAL := check

.PHONY: setup format format-check lint typecheck test check build docker-build docker-dev up stats

setup:
	uv sync --locked

format:
	uv run --locked ruff format .

format-check:
	uv run --locked ruff format --check .

lint:
	uv run --locked ruff check .

typecheck:
	uv run --locked ty check

test:
	uv run --locked pytest

check: format-check lint typecheck

build:
	uv build --no-sources

docker-build:
	docker build --target runtime --tag telegram-stream .

docker-dev:
	docker build --target development --tag telegram-stream:dev .

up:
	uv run --locked python scripts/compose_up.py $(ARGS)

stats:
	uv run --locked python scripts/resource_stats.py $(ARGS)
