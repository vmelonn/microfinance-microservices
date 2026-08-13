"""
A service that traces must be able to import the client.

Five services shipped reading REDIS_URL, calling trace.configure, and having
no `redis` package in their image. The failure is the quietest possible one:
one WARNING at startup,

    tracing disabled, Redis unreachable: No module named 'redis'

then perfect service and total absence from every trace. It looked exactly
like a Redis outage and cost a full diagnostic round trip to tell apart.

This is a static check over the requirements files rather than a runtime one,
because the runtime symptom is a service behaving correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICES = sorted(p for p in (ROOT / "services").iterdir()
                  if p.is_dir() and not p.name.startswith("_"))

BASE_REQUIREMENTS = (ROOT / "services" / "_base" / "requirements.txt").read_text(
    encoding="utf-8")


def _reads_redis_url(service: Path) -> bool:
    return any("REDIS_URL" in f.read_text(encoding="utf-8")
               for f in (service / "app").glob("*.py"))


def _can_import_redis(service: Path) -> bool:
    """From its own requirements, or inherited from the shared base image."""
    own = service / "requirements.txt"
    own_text = own.read_text(encoding="utf-8") if own.exists() else ""
    return any(line.strip().startswith("redis")
               for line in (own_text + "\n" + BASE_REQUIREMENTS).splitlines())


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_a_service_that_traces_can_import_the_client(service):
    if not _reads_redis_url(service):
        pytest.skip(f"{service.name} does not trace")
    assert _can_import_redis(service), (
        f"{service.name} reads REDIS_URL but neither its requirements.txt nor "
        f"the shared base installs `redis`. It will start fine, serve traffic "
        f"fine, and never appear in a single trace."
    )


def test_the_base_carries_redis():
    """It is in the base rather than in seven files because seven of nine
    services need it, which is the rule that file states for itself."""
    assert any(line.strip().startswith("redis")
               for line in BASE_REQUIREMENTS.splitlines()), \
        "the shared base no longer installs redis; every tracing service needs it"
