# microfinance-microservices

A mobile money platform on OpenShift. Eight services, one repository, a
Dockerfile per service. A payment travels **REST into the gateway, SOAP into
an IBM ACE gateway, BCD-packed ISO 8583 over a socket to the switch, and back
up the same path**.

The phone number is the account. There is an operator console at the Route
root: wallet, live per-request tracing, a ledger browser, and a load test.

> **Live console:** add your Route URL here after the first deploy, and to the
> repository's **About → Website** field so it appears at the top of the page.
> `https://api-gateway-reemostat-dev.apps.rm2.thpm.p1.openshiftapps.com/`

Full reference, including 22 scenarios recorded from the services' own logs:
[docs/architecture.html](docs/architecture.html).

---

## The layers, and how they connect

```
                   mobile client / operator console
                                 |  REST + JWT
                    +------------v------------+
                    |       api-gateway       |  the only Route
                    |  auth, idempotency, CID |
                    +--+--------+---------+---+
                       |        |         |
              +--------v-+  +---v------+  +--v-------------+
              |   auth   |  |  ledger  |  |  transaction   |  the saga
              | Postgres |  | Postgres |  |  orchestrator  |
              +----------+  +----------+  +--+----------+--+
                                             |          |
                                    +--------v-+   +----v-----------+
                                    |   risk   |   | iso8583-adapter|
                                    |  Redis   |   | PIN block, HSM |
                                    +----------+   +----+-----------+
                                                        |  SOAP 1.1
                                                   +----v-----------+
                                                   | IBM ACE gateway|
                                                   | DFDL <-> ISO   |
                                                   +----+-----------+
                                                        |  ISO 8583 / TCP
                                                   +----v-----------+
                                                   | host-simulator |
                                                   +----------------+
```

Every service tags its logs and trace events with the **correlation ID** the
gateway mints, which is what lets the console show one request's whole path.

| Service | Port | Owns |
|---|---|---|
| api-gateway | 8080 | JWT, idempotency claims, the console. Only public Route |
| auth-service | 8081 | Users, bcrypt hashes, JWT issuing. Own Postgres |
| transaction-service | 8082 | The saga and its compensations |
| risk-service | 8083 | Velocity, amount and entry-mode rules. Redis |
| ledger-service | 8084 | Accounts, cards, double-entry postings, solvency. Own Postgres |
| iso8583-adapter | 8085 | PIN block, HSM, the SOAP client |
| ace-stub | 8090 | Stands in for IBM ACE, serves the identical WSDL |
| host-simulator | 9999 | A fake switch, deployed as a real service |

---

## One payment, end to end

Recorded, not drawn. This is a real trace from the console:

```
api-gateway          gateway   POST /transactions/purchase
api-gateway          gateway   idempotency key claimed
transaction-service  ->        ledger  POST /internal/ledger/resolve   200
transaction-service  ->        ledger  GET  .../balance                200   solvency
transaction-service  ->        risk    POST /internal/risk/evaluate    200
risk-service         risk      approve: no rule fired
transaction-service  saga      RRN generated, entering the point of no return
iso8583-adapter      security  PIN block built  (ISO 9564 format 0)
iso8583-adapter      soap      SOAP authorizeRequest to the ISO 8583 gateway
ace-stub             iso8583   built MTI 0200  DE [2,3,4,22,37,49,52,53]
ace-stub             switch    switch answered MTI 0210  de39=00  de38=A18008
iso8583-adapter      soap      SOAP response: approved
ledger-service       ledger    double-entry posting: recorded
api-gateway          gateway   responded 200
```

Order matters. Solvency and risk are checked **before** the switch, so an
unaffordable or refused payment never produces an authorisation that would
then have to be reversed.

---

## Money rules

Every wallet opens at **zero** and cannot be overdrawn. Solvency is enforced
inside the same database transaction as the insert, with a row lock, because
a check-then-write races.

Money enters through `acc_system_funding`. Crediting a customer means
debiting something, so a top-up debits that account and credits the wallet.
Its balance is **negative by design**: its magnitude is the float customers
hold, and it is the figure a treasury team reconciles.

Outcomes are three-valued. A timeout is **unknown**, not declined: the switch
may have approved. Reporting a decline for a lost response quietly takes
someone's money.

---

## Quickstart

Windows: use **Git Bash** for `make`. See [CLAUDE.md](CLAUDE.md).

```bash
make install
make verify      # 8 services as processes, full path proven, no Docker needed
make run         # same, left running on http://127.0.0.1:18080
make test        # 173 tests
make scenarios   # 22 situations, recording what each layer did
```

`make run` starts an embedded Redis so the console's Live trace works with no
setup.

---

## OpenShift

```bash
oc login ...                                  # copy from the web console
oc apply -f openshift/build/build.yaml        # ImageStreams + BuildConfigs
bash scripts/openshift-build.sh               # build all nine images in-cluster
oc apply -f openshift/secret-template.yaml    # real secrets, placeholders ship
oc apply -k openshift/overlays/dev
```

After that, pushing to `main` deploys: GitHub Actions builds in the cluster,
waits for the rollout, and runs the smoke test against the Route.

Things that bite on a constrained namespace:

- **`revisionHistoryLimit: 1`.** The default of 10, times nine services, is
  90 ReplicaSets and a namespace that refuses to schedule.
- **No hardcoded `fsGroup`.** `restricted-v2` allocates one and rejects
  fixed values.
- **Build memory.** Nine parallel 2Gi builds exceed a 14Gi quota and hang
  for hours rather than failing.
- **A rollout is not complete when `readyReplicas` matches.** That counts
  old pods too. The gate also requires `status.replicas`, or the smoke test
  runs against the pod being replaced.

---

## Testing

| Command | What it proves |
|---|---|
| `make test` | 173 unit and integration tests |
| `make verify` | the full REST to ISO 8583 path, as processes |
| `make smoke` | the same against compose, including cross-replica idempotency |
| `make scenarios` | 22 situations, with layers harvested from service logs |
| `make ui-test` | the console driven in a real DOM against a running stack |

The scenario runner is the unusual one: it drives the platform from outside
and reads back what each service logged, so the flows in the architecture doc
are recordings rather than drawings.

---

## Layout

```
services/          8 services, a Dockerfile each, plus a shared _base image
libs/mfcommon/     ISO 8583, SOAP, PIN blocks, MSISDN, db dialect, tracing
ace/               WSDL, DFDL schema, ESQL. ace-stub serves the same contract
openshift/         kustomize base and dev/prod overlays
scripts/           run_local, smoke_test, scenarios
docs/              architecture.html
```
