"""
The inter-service HTTP client.

The monolith called risk.evaluate() and either got an answer or got an
exception. Across a network, there is a third outcome that has no
single-process equivalent: the call may have SUCCEEDED and the response
lost. That distinction is the whole reason this file is more than a thin
httpx wrapper.

Three behaviours, and the reasoning for each:

1. TIMEOUTS ARE MANDATORY, not optional.
   httpx defaults to a 5s timeout, but a service that forgets to set one
   explicitly inherits whatever the default happens to be that release. A
   downstream service that hangs must not be able to hang its caller too --
   that is how one slow pod takes down a whole platform. Every call here
   has an explicit deadline.

2. RETRIES ONLY ON IDEMPOTENT OPERATIONS.
   Retrying a GET is free. Retrying "post these ledger entries" can move
   money twice. The retry policy is therefore opt-in per call, never a
   blanket default, and the only POSTs that opt in are the ones carrying an
   RRN or idempotency key that makes them safe -- exactly the guarantee
   ledger-service's PRIMARY KEY (rrn) provides.

3. CIRCUIT BREAKING.
   When a downstream is already failing, continuing to send it traffic
   makes recovery slower and burns the caller's own workers waiting on
   doomed calls. After N consecutive failures the breaker opens and calls
   fail immediately for a cooldown, then a single probe decides whether to
   close it again.
"""

from __future__ import annotations

import threading
import time

from mfcommon.observability.correlation import outbound_headers


class ServiceCallError(Exception):
    """A downstream service could not be reached, or returned 5xx."""

    def __init__(self, service: str, message: str, status_code: int | None = None):
        self.service = service
        self.status_code = status_code
        super().__init__(f"[{service}] {message}")


class ServiceRejectedError(Exception):
    """
    A downstream returned a 4xx -- it understood the request and refused it.
    Kept separate from ServiceCallError because the two need opposite
    handling: a 4xx must NEVER be retried (the answer will not change) and
    should usually be surfaced to the caller as-is, while a 5xx may be
    transient.
    """

    def __init__(self, service: str, status_code: int, detail):
        self.service = service
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"[{service}] rejected with {status_code}: {detail}")


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                # Half-open: let exactly one probe through. If it succeeds,
                # record_success() closes the breaker; if it fails,
                # record_failure() restarts the cooldown.
                self._opened_at = None
                self._failures = self.failure_threshold - 1
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()


class ServiceClient:
    """
    One instance per downstream service, held for the process lifetime --
    the breaker state and the connection pool are both per-downstream, and
    both are meaningless if you build a fresh client per request.
    """

    def __init__(
        self,
        service_name: str,
        base_url: str,
        timeout: float = 5.0,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
    ):
        import httpx

        self.service_name = service_name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.breaker = CircuitBreaker(failure_threshold, reset_timeout)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            # Bounded pool: an unbounded one just relocates a downstream
            # slowdown into unbounded memory growth in the caller.
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, *, retries: int = 2, timeout: float | None = None, **kwargs):
        return self._request("GET", path, retries=retries, timeout=timeout, **kwargs)

    def post(self, path: str, json: dict, *, retries: int = 0, timeout: float | None = None, **kwargs):
        """
        retries defaults to 0 -- POSTs are assumed unsafe to repeat unless
        the caller explicitly says otherwise. Pass retries>0 ONLY when the
        endpoint is idempotent on a key in the body (an RRN, an idempotency
        key). Getting this wrong duplicates money movement.
        """
        return self._request("POST", path, json=json, retries=retries, timeout=timeout, **kwargs)

    def _request(self, method: str, path: str, *, retries: int, timeout: float | None, **kwargs):
        import httpx

        if self.breaker.is_open:
            raise ServiceCallError(
                self.service_name,
                f"circuit breaker is open -- not attempting {method} {path}",
            )

        headers = outbound_headers(kwargs.pop("headers", None))
        attempt = 0
        last_error: Exception | None = None

        while attempt <= retries:
            try:
                response = self._client.request(
                    method, path, headers=headers, timeout=timeout or self.timeout, **kwargs
                )
            except httpx.RequestError as exc:
                # Transport-level: connection refused, DNS failure, read
                # timeout. For a POST this is the genuinely ambiguous case --
                # the request may well have been processed.
                last_error = ServiceCallError(self.service_name, f"transport error: {exc!r}")
                self.breaker.record_failure()
                attempt += 1
                if attempt <= retries:
                    time.sleep(min(0.1 * (2 ** attempt), 2.0))  # exponential backoff
                continue

            if response.status_code >= 500:
                last_error = ServiceCallError(
                    self.service_name,
                    f"{method} {path} returned {response.status_code}",
                    response.status_code,
                )
                self.breaker.record_failure()
                attempt += 1
                if attempt <= retries:
                    time.sleep(min(0.1 * (2 ** attempt), 2.0))
                continue

            self.breaker.record_success()

            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail", response.text)
                except Exception:
                    detail = response.text
                raise ServiceRejectedError(self.service_name, response.status_code, detail)

            return response.json()

        raise last_error  # every attempt failed
