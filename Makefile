ifeq ($(OS),Windows_NT)
  SYSTEM_PY ?= py -3
  PY ?= .venv/Scripts/python.exe
else
  SYSTEM_PY ?= python3
  PY ?= .venv/bin/python
endif

.PHONY: install setup run test coverage lint format check docker

install:
	$(SYSTEM_PY) -m venv .venv
	$(PY) -m pip install -r requirements-dev.txt

setup:
	$(PY) manage.py migrate
	$(PY) manage.py load_stations --if-empty

run:
	$(PY) manage.py runserver

test:
	$(PY) manage.py test fuelplanner

coverage:
	$(PY) -m coverage run manage.py test fuelplanner
	$(PY) -m coverage report

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

check: lint test
	$(PY) manage.py makemigrations --check --dry-run
	$(PY) manage.py spectacular --validate --fail-on-warn --file .openapi-check.yml

docker:
	docker compose up --build
