.PHONY: install dev test test-cov lint format run check init-db once docker-build docker-up docker-down clean

install:
	pip install -e .

dev:
	pip install -e ".[dev,api]"

test:
	SEEQL_ENV=test pytest tests/ -v --tb=short

test-cov:
	SEEQL_ENV=test pytest tests/ -v --cov=config --cov=collectors --cov=storage --cov=parsers --cov=scheduler --cov=api --cov-report=term-missing

lint:
	ruff check . && ruff format --check .

format:
	ruff format .

run:
	python main.py

check:
	python main.py --check

init-db:
	python main.py --init-db

once:
	python main.py --once

docker-build:
	docker build -t seeql .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf __pycache__ .pytest_cache *.egg-info build dist
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
