# microfinance-microservices

A card/account transaction platform: **REST on the outside, SOAP at the
integration boundary, ISO 8583 on the wire.** Seven services plus two test
doubles, one repository, one Dockerfile per service, deployed to OpenShift.

This is the decomposition of [`microfinance-stack`](../microfinance-stack) —
the same eight layers, split along ownership boundaries and given the
machinery a distributed system needs and a monolith does not.

```
                                          ┌─────────────┐
  mobile client ──REST/JSON──▶ api-gateway│  the only   │
                                  │       │   Route     │
                                  │       └─────────────┘
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              auth-service  transaction-  ledger-service
                            service            │
                                  │            │ Postgres
                    ┌─────────────┤
                    ▼             ▼
              risk-service   iso8583-adapter ◀── the crypto edge
                    │             │
                  Redis           │  ══ SOAP 1.1 / WSDL ══▶  IBM ACE
                                  │                            │  DFDL
                                  │                            ▼
                                  │                    ISO 8583 over TCP
                                  │                            │
                                  ◀════ SOAP response ═════ the switch
```

One SOAP hop, exactly at the ACE boundary. Everything north of it is
REST/JSON; everything south is binary ISO 8583.

## Start here

```bash
make install          # local venv
make test             # every suite — 67 tests
make verify           # start all 8 services, prove the full path, tear down
```

`make verify` needs **no container engine**. It launches every service as an
ordinary process and drives a real purchase from outside — REST into the
gateway, SOAP into the ISO 8583 gateway, real BCD-packed binary over a real
socket to the switch, and back:

```
starting host-simulator         :9999   ready
starting ace-stub               :8090   ready
starting iso8583-adapter        :8085   ready
starting ledger-service         :8084   ready
starting auth-service           :8081   ready
starting risk-service           :8083   ready
starting transaction-service    :8082   ready
starting api-gateway            :18080  ready

  PASS  purchase approved
  PASS  posted to the ledger          rrn=178638403660  stan=000017  auth=A18008
  PASS  balance is -2550 cents
  PASS  replay returned the cached result
  PASS  balance unchanged after the replay
  PASS  mismatched body rejected
  PASS  cannot spend from someone else's card
        approved → approved → approved → review → review → decline → decline
  PASS  velocity escalated to review or decline
  PASS  total debits == total credits
```

For the full thing — Postgres, Redis, ClickHouse, and **two** gateway
replicas:

```bash
make up               # 9 services, 3 datastores
make smoke
```

Compose is what demonstrates the one thing `make verify` cannot: that a retry
landing on a *different* gateway replica returns the cached result instead of
charging twice. That behaviour depends on shared Redis state, so it needs
more than one process to be meaningful.

## The services

| Service | Port | Owns | Replicas |
|---|---|---|---|
| **api-gateway** | 8080 | The only Route. JWT verification, idempotency claims, correlation IDs | 2+ |
| **auth-service** | 8081 | Users, passwords, JWT issuance. Its own Postgres | 2 |
| **transaction-service** | 8082 | Saga orchestration, RRN generation, compensation | 2 |
| **risk-service** | 8083 | Velocity, amount, entry-mode rules. Redis-backed | 2 |
| **ledger-service** | 8084 | Double-entry ledger. Its own Postgres | 2 |
| **iso8583-adapter** | 8085 | **REST→SOAP boundary.** PIN blocks, HSM, reversals | **1** |
| **analytics-sync** | — | CronJob: ledger → ClickHouse | — |
| *ace-stub* | 8090 | Test double: serves the real WSDL | 1 |
| *host-simulator* | 9999 | Test double: fake switch | 1 |

**iso8583-adapter runs one replica, deliberately.** Each pod derives PIN keys
from its own persisted base key, so two replicas hold *different* keys and a
PIN block encrypted by one cannot be decrypted by the other. Scaling it out
needs a shared KMS-backed key, not a higher replica count.

## IBM ACE, and why nothing waits for it

The entitlement key had not come through. Rather than leave the riskiest
integration in the platform untested until the licence landed,
`services/ace-stub/` implements the **same WSDL** in Python — and it is a
stand-in, not a mock. It builds real BCD-packed messages with a real bitmap,
opens a real TCP socket to the switch, and parses the real binary response.

Swapping in the real thing is one environment variable:

```bash
ISO8583_SOAP_ENDPOINT=http://ace:7800/Iso8583Gateway
```

No application code changes. Every test is written against the WSDL contract
rather than against either implementation, so a green run after the swap is
genuine evidence it worked.

The ACE artifacts — DFDL schema, ESQL, message-flow spec, Dockerfile — are in
[`ace/`](ace/), with an honest status table for each. See [ace/README.md](ace/README.md).

### The conformance obligation

`ace/Iso8583Library/dfdl/ISO8583.xsd` and
`libs/mfcommon/mfcommon/iso8583/parser.py` are two independent
implementations of the same binary format. They will drift unless something
forces them together. `tests/e2e/test_dfdl_conformance.py` is that forcing
function — 23 assertions pinning BCD padding, odd-length filler nibbles,
LLVAR digit-vs-byte counts, bitmap bit positions, and the no-trim rule on
DE 52 and DE 64.

## What changed from the monolith

The eight layers survive; what changed is everything a network forces you to
confront.

| Monolith | Here | Why |
|---|---|---|
| One function call | Seven HTTP hops | Independent deploys and scaling |
| Python exception unwinds everything | **Saga with explicit compensation** | No distributed transaction exists |
| One SQLite file | Postgres per service | Independent schema ownership |
| One traceback | **Correlation IDs** through every hop, including SOAP | One request is now seven log streams |
| `switch/client.py` over TCP | **SOAP → ACE → ISO 8583** | Protocol mediation belongs in an ESB |
| In-process risk state | Redis | Per-pod state makes velocity rules bypassable |
| Direct function call | Timeouts, selective retries, circuit breakers | A call can now succeed *and* be lost |
| Redshift (planned) | **ClickHouse** | Self-hostable, so it is actually tested |

### The three-valued outcome

The monolith had approved and declined. This platform has **approved,
declined, and unknown** — because a network call can succeed while its
response is lost.

When the switch times out, the money may already have moved. Returning
"declined" would be a lie that loses a customer's money silently. So
`iso8583-adapter` returns `outcome="unknown"`, `transaction-service` declines
to post to the ledger, a reversal is issued, and the transaction is flagged
`requires_reconciliation`. That state did not exist in the monolith and it is
the main thing the decomposition costs.

### The one guarantee everything rests on

`PRIMARY KEY (rrn)` on `transactions`. The gateway's idempotency claim, the
saga's retry policy, the reversal-on-timeout — all of it is best-effort. That
constraint is enforced by the database in a single atomic statement and holds
however many replicas race. `services/ledger-service/tests/test_ledger.py`
proves it with ten threads on a barrier.

## Repository layout

```
libs/mfcommon/          shared: ISO 8583 codec, SOAP envelopes, JWT, PIN blocks,
                        correlation IDs, audit masking, resilient HTTP client
services/<name>/        app/ + tests/ + Dockerfile + requirements.txt
ace/                    DFDL schema, WSDL, ESQL, flow spec, Dockerfile
openshift/base/         Deployments, Services, Route, NetworkPolicies, HPAs, CronJobs
openshift/overlays/     dev and prod kustomize overlays
tests/e2e/              cross-service and DFDL conformance
scripts/smoke_test.py   end-to-end drive from outside
docs/architecture.html  full flows, layers, and API reference
```

`libs/mfcommon` holds things that must be **byte-identical** across services —
if the adapter and the stub disagree about BCD padding by one nibble, every
message corrupts. Business rules deliberately stay out: risk thresholds live
in risk-service, accounting rules in ledger-service. A shared library that
accumulates business logic is how a microservice split quietly becomes a
distributed monolith.

## Deploying to OpenShift

Images are built **inside the cluster** from an upload of the working tree,
so no local container engine is required.

```bash
# 0. log in — copy the command from the console (your name → Copy login command)
oc login --token=sha256~… --server=https://api.your-cluster:6443
oc new-project microfinance-dev

# 1. ImageStreams + BuildConfigs
make oc-init

# 2. build all nine images in the cluster (a few minutes the first time)
make oc-build

# 3. real secrets — the repo ships placeholders on purpose
oc create secret generic microfinance-secrets \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 16)" \
  --from-literal=HSM_MASTER_KEY_HEX="$(openssl rand -hex 32)" \
  --from-literal=CLICKHOUSE_PASSWORD="" \
  --dry-run=client -o yaml | oc apply -f -

# 4. deploy
make deploy-dev
make oc-status
```

Then drive a real purchase against the Route:

```bash
make oc-smoke
```

### Two things that will bite otherwise

**Image names must resolve to ImageStreams.** `image: api-gateway:latest` is
not a public image — without help, OpenShift looks it up in Docker Hub and
every pod lands in `ImagePullBackOff`. Two settings fix it together, and both
are already in the manifests: `lookupPolicy.local: true` on each ImageStream,
and `alpha.image.policy.openshift.io/resolve-names: '*'` on each pod template.
Miss either and the symptom is identical.

**Arbitrary UIDs.** OpenShift runs containers as a random UID, not the one in
the image. Every Dockerfile installs packages system-wide rather than into a
named user's home and runs `chgrp -R 0 /app && chmod -R g=u /app`. Getting
this wrong surfaces as `executable file not found in $PATH` — not a
permission error — which sends you looking in entirely the wrong place.

### Where the images come from

The BuildConfigs use **Git source** — the cluster clones this repo and builds
from it, so every image is traceable to a commit:

```bash
oc describe bc/api-gateway | grep -i 'commit\|ref'
```

#### Why not the console's "Import from Git", or `oc new-app`?

**You can use Import from Git for the builds** — set *Context dir* to `/` and
*Dockerfile path* to `services/<name>/Dockerfile`. Both must be set: every
Dockerfile needs the **repository root** as its build context, because each
image installs `libs/mfcommon` as a real package. Pointing the context at
`services/<name>/` builds a tree with no `libs/` in it and the build fails on
the `COPY`.

`oc new-app --context-dir=…` genuinely cannot express this — that one flag
sets the context *and* the Dockerfile location together, and there is no
`--dockerfile-path` to separate them. The console form has the two as
separate fields.

**What neither generates is the deployment topology.** Import from Git
creates a Deployment, a Service, and a Route *per application*. This platform
needs exactly one Route — `ledger-service` and `iso8583-adapter` reachable
from the internet is a real problem, not a cosmetic one — plus
NetworkPolicies, two CronJobs, a PVC for the HSM key, the ConfigMap and
Secret, and the single-replica pin on the adapter.

So: use the console for builds if you prefer clicking, then `oc apply -k` for
the deploy. Or skip the nine wizard runs and apply `build.yaml`, which is the
same nine builds in one command.

### Rebuilding after a change

```bash
git push                                          # then:
bash scripts/openshift-build.sh api-gateway       # rebuild from Git
oc rollout restart deployment/api-gateway
```

While iterating on something uncommitted, `--local` uploads your working tree
instead:

```bash
bash scripts/openshift-build.sh --local api-gateway
```

That image corresponds to no commit, so it's for iteration only — the script
warns when your tree is dirty.

Optionally, register a GitHub webhook so a push rebuilds automatically; the
instructions and the secret are at the bottom of
[`openshift/build/build.yaml`](openshift/build/build.yaml). Nine services
means nine webhooks, so an explicit `make oc-build` is a reasonable choice
for a repo this size.

### Before production

Read the header of
[`openshift/overlays/prod/kustomization.yaml`](openshift/overlays/prod/kustomization.yaml).
Three things must be true: real secrets in place, `SWITCH_HOST` repointed at
the real acquirer (the prod overlay **deletes** host-simulator), and image
tags pinned rather than `latest`.

Also switch the BuildConfigs from binary to Git source. Binary builds deploy
whatever is in your working tree, including uncommitted changes — useful
while iterating, and exactly wrong for something you need to trace back to a
commit.

## Known gaps

Stated plainly rather than discovered later:

1. **No MAC on outbound messages.** DE 64/128 are modelled and masked but never
   generated. Most real switches require one.
2. **DE 90 uses placeholder institution IDs** in reversals. The simulator only
   needs MTI and STAN; a real switch will want the genuine values echoed.
3. **No RBAC.** `/internal/ledger/reset` is config-gated, not
   authorization-gated. Any authenticated user is equivalent to any other.
4. **`MockHSM` is XOR**, not a real HSM. Same interface, no tamper resistance.
   Inherited from the monolith and clearly labelled there too.
5. **Plaintext PIN crosses one internal hop** — gateway to adapter. Mitigated by
   a NetworkPolicy; a real PCI environment wants mTLS there.
6. **RRN collision is possible** — 10 digits of epoch seconds plus 2 random
   means a 1-in-100 chance within the same second. The ledger's PRIMARY KEY
   turns a collision into a rejected duplicate rather than corrupted money,
   but the format should change before real volume.
