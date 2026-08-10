# microfinance-microservices
#
#   make install    set up the local venv
#   make test       run every test suite
#   make up         boot the whole platform locally
#   make smoke      end-to-end purchase against a running platform
#   make deploy-dev push to OpenShift

SHELL := /bin/bash
PY    := .venv/bin/python
PIP   := .venv/bin/pip

# Windows venvs put executables in Scripts/ rather than bin/.
ifeq ($(OS),Windows_NT)
	PY  := .venv/Scripts/python.exe
	PIP := .venv/Scripts/pip.exe
endif

SERVICES := api-gateway auth-service transaction-service risk-service \
            ledger-service iso8583-adapter ace-stub host-simulator analytics-sync

# Services that have a tests/ directory worth running.
TESTED := ace-stub ledger-service

.DEFAULT_GOAL := help
.PHONY: help install test test-shared test-services lint up down logs smoke \
        build deploy-dev deploy-prod clean wsdl

help:
	@echo "microfinance-microservices"
	@echo ""
	@echo "  install       create .venv and install mfcommon + dev deps"
	@echo "  test          run every suite (shared + per-service)"
	@echo "  up            docker compose up --build"
	@echo "  down          stop and remove containers"
	@echo "  smoke         end-to-end purchase against localhost:8080"
	@echo "  build         build all service images"
	@echo "  deploy-dev    oc apply -k openshift/overlays/dev"
	@echo "  wsdl          print the service contract"

install:
	python -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e "libs/mfcommon[dev]"
	$(PIP) install --quiet fastapi "uvicorn[standard]" httpx pydantic redis cryptography clickhouse-connect
	@echo "ready. run 'make test'"

# Each service owns a top-level `app` package, so they cannot share one
# sys.path -- two services' app.main would shadow each other. Hence one
# pytest invocation per service.
test: test-shared test-services

test-shared:
	@echo "=== shared: mfcommon + conformance ==="
	@$(PY) -m pytest libs/mfcommon/tests tests -q

test-services:
	@for svc in $(TESTED); do \
		echo "=== $$svc ==="; \
		PYTHONPATH="services/$$svc" \
		WSDL_PATH="ace/Iso8583Library/wsdl/Iso8583Gateway.wsdl" \
		$(PY) -m pytest "services/$$svc/tests" -q -p no:cacheprovider || exit 1; \
	done

lint:
	@$(PY) -m compileall -q libs services && echo "syntax OK"

up:
	docker compose up --build

down:
	docker compose down

# Wipes the volumes too. Postgres init scripts only run on an empty volume,
# so this is what you need after changing scripts/init-databases.sh.
clean:
	docker compose down -v

logs:
	docker compose logs -f --tail=100

smoke:
	@$(PY) scripts/smoke_test.py

wsdl:
	@cat ace/Iso8583Library/wsdl/Iso8583Gateway.wsdl

build:
	@for svc in $(SERVICES); do \
		echo "=== building $$svc ==="; \
		docker build -f "services/$$svc/Dockerfile" -t "$$svc:latest" . || exit 1; \
	done

deploy-dev:
	oc apply -k openshift/overlays/dev

deploy-prod:
	@echo "Before applying: real secrets in place, SWITCH_HOST repointed, images pinned."
	@echo "See the header of openshift/overlays/prod/kustomization.yaml."
	@read -p "Proceed? [y/N] " ok && [ "$$ok" = "y" ]
	oc apply -k openshift/overlays/prod
