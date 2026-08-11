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
        run verify build build-base deploy-dev deploy-prod clean wsdl \
        oc-init oc-build oc-status oc-smoke

help:
	@echo "microfinance-microservices"
	@echo ""
	@echo "  install       create .venv and install mfcommon + dev deps"
	@echo "  test          run every suite (shared + per-service)"
	@echo "  verify        start all 8 services as processes, prove the"
	@echo "                REST->SOAP->ISO 8583->REST path, tear down."
	@echo "                No Docker required."
	@echo "  run           same, but leave it running on :18080"
	@echo "  up            docker compose up --build (needs a container engine)"
	@echo "  down          stop and remove containers"
	@echo "  smoke         end-to-end purchase against a running compose stack"
	@echo "  build         build all service images"
	@echo ""
	@echo "  OpenShift (no local container engine needed):"
	@echo "  oc-init       create ImageStreams + BuildConfigs"
	@echo "  oc-build      build all 9 images IN the cluster"
	@echo "  deploy-dev    oc apply -k openshift/overlays/dev"
	@echo "  oc-status     pods, routes, imagestreams, recent builds"
	@echo "  oc-smoke      drive a purchase against the deployed Route"
	@echo "  wsdl          print the service contract"

install:
	python -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e "libs/mfcommon[dev]"
	$(PIP) install --quiet fastapi "uvicorn[standard]" httpx pydantic redis cryptography clickhouse-connect
	@echo "ready. run 'make test'"

# Each service owns a top-level `app` package, so they cannot share one
# sys.path, two services' app.main would shadow each other. Hence one
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

# The whole platform as plain processes, no container engine needed.
# SQLite stands in for Postgres, in-memory state for Redis, so this cannot
# demonstrate cross-replica behaviour. Use `make up` for that.
verify:
	@$(PY) scripts/run_local.py --verify

run:
	@$(PY) scripts/run_local.py

# compose builds services in parallel and does NOT order builds by
# depends_on, so the base has to exist before it starts.
up: build-base
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

# The shared base. Every service Dockerfile starts FROM this, so it has to
# exist before any of them can build. Slow the first time (it compiles
# uvloop, httptools and friends), then cached.
build-base:
	@echo "=== building microfinance-base ==="
	docker build -f services/_base/Dockerfile -t microfinance-base:latest .

build: build-base
	@for svc in $(SERVICES); do \
		echo "=== building $$svc ==="; \
		docker build -f "services/$$svc/Dockerfile" -t "$$svc:latest" . || exit 1; \
	done

# --- OpenShift -------------------------------------------------------------
# The full first-time sequence is: oc-init, oc-build, deploy-dev.
# None of it needs a local container engine, builds happen in the cluster.

oc-init:
	@oc whoami >/dev/null 2>&1 || { echo "not logged in, run the 'oc login' command from the console"; exit 1; }
	oc apply -f openshift/build/build.yaml
	@echo "ImageStreams and BuildConfigs created. Next: make oc-build"

oc-build:
	bash scripts/openshift-build.sh

oc-status:
	@echo "--- pods ---";        oc get pods
	@echo "--- routes ---";      oc get route
	@echo "--- imagestreams ---"; oc get is
	@echo "--- recent builds ---"; oc get builds --sort-by=.metadata.creationTimestamp | tail -10

# Drives a real purchase against the deployed Route.
oc-smoke:
	@$(PY) scripts/smoke_test.py --base "https://$$(oc get route api-gateway -o jsonpath='{.spec.host}')"

deploy-dev:
	oc apply -k openshift/overlays/dev

deploy-prod:
	@echo "Before applying: real secrets in place, SWITCH_HOST repointed, images pinned."
	@echo "See the header of openshift/overlays/prod/kustomization.yaml."
	@read -p "Proceed? [y/N] " ok && [ "$$ok" = "y" ]
	oc apply -k openshift/overlays/prod
