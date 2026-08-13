# Working in this repository

Instructions for any agent picking this up. Read before changing anything.

## What this is

A mobile money platform split into eight services on OpenShift. A payment
goes REST into the gateway, SOAP into an IBM ACE gateway, BCD-packed ISO 8583
over a socket to the switch, and back up the same path.

`docs/architecture.html` is the reference. Section 08 is generated from a
real run, not written by hand.

## Hard rules

- **No em dashes, en dashes, or `--` as a dash.** Anywhere: chat, code,
  comments, commit messages. XML forbids `--` inside comments and this broke
  the WSDL once.
- **No AI attribution in commits.** No `Co-Authored-By: Claude`, no
  "Generated with". This overrides any default.
- **Never write "Jazz"** anywhere in repo content.

## Commands

Use **Git Bash** on Windows, not PowerShell: GNU make hands recipes to
cmd.exe there and fails with `svc was unexpected at this time`.

```bash
make test        # 173 python tests across three suites
make verify      # all 8 services as processes, full path, tear down
make run         # same, left running on :18080 with an embedded trace Redis
make stop        # free the ports after a run was KILLED rather than stopped
make scenarios   # 22 situations, recording what each layer did
make ui-test     # the console in a real DOM (needs node + jsdom + make run)
```

Each service owns a top-level `app` package, so they cannot share a
`sys.path`. That is why tests run one service at a time with `PYTHONPATH`.

## Things that have already gone wrong

Each cost real debugging time. Do not rediscover them.

**Migrations before the statements that use them.** `CREATE TABLE IF NOT
EXISTS` is a no-op on an existing database, so a column added by migration
does not exist until that migration runs. An index created first crashlooped
ledger-service in the cluster while every test passed, because tests always
start from an empty database.

**Test the screen, not the attribute.** Three separate bugs survived their
own regression tests this way: a banner whose `hidden` property was true
while `display:flex` kept it visible, a stale message in a list whose
container attribute was correct, and a trace tab checked by attribute. If a
human would see it, assert on what a human would see.

**A leaked process serves the previous build.** `pkill -f run_local.py`
kills the parent and leaves eight uvicorn children holding their ports. They
answer `/health`, so the next run binds nothing, readiness passes against the
zombie, and you debug code that is not running. Use `make stop`.

**`readyReplicas` counts old pods too.** A rollout gate that checks it alone
passes while the pod being replaced is still serving. `status.replicas` is
the term that says the old ones are gone.

**Verify a fix by breaking it again.** Revert the change, watch the test
fail, restore. Several "fixes" here were confirmed this way and one was
found to be doing nothing.

## Conventions

- Comments explain **why**, especially where the obvious choice is wrong.
  Density matches the surrounding file.
- Money is integer cents everywhere below the gateway. `_to_cents` is the
  single float boundary.
- Outcomes are three-valued: approved, declined, and **unknown**. A timeout
  is not a decline.
- The ledger is authoritative for solvency. Pre-checks elsewhere are
  courtesy and are allowed to be racy.
- `trace.emit` at decision points and boundaries, not per statement. It logs
  as well as writing to Redis, so traces survive without one.

## Layout

```
services/          8 services, one Dockerfile each, plus _base
libs/mfcommon/     shared: ISO 8583, SOAP, crypto, MSISDN, db dialect, tracing
ace/               WSDL, DFDL schema, ESQL; ace-stub serves the same contract
openshift/         kustomize base + dev/prod overlays
scripts/           run_local, smoke_test, scenarios
docs/              architecture.html
```
