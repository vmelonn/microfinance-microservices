"""
host-simulator, a fake payment switch, deployed as a real service.

In the monolith this ran in-process, started by the API's own lifespan
(RUN_LOCAL_SIMULATOR=1). That worked because there was one process. Here it
has to be a separate deployable, because ACE connects to it over TCP from
another pod and cannot start something inside a Python process it does not
share.

Promoting it to a real service is also more honest. In production this pod
is simply not deployed, and SWITCH_HOST points at the acquirer's actual
endpoint instead. Nothing else in the platform can tell the difference,
which is the property that makes the test environment worth trusting.

Responses:
    0800 -> 0810   network management acknowledged
    0200 -> 0210   approved, DE 39 = 00
    0400 -> 0410   reversal acknowledged
    anything else  -> silence, so timeout paths have something to time out
                      against

Test hooks, both driven by DE 48:
    DE 48 = "SIMULATE_TIMEOUT"  -> no response at all
    DE 48 = "DELAY:2.5"         -> respond after 2.5 seconds
"""

import os
import signal
import sys
import threading

from mfcommon.iso8583.host_simulator import HostSimulator
from mfcommon.observability.correlation import configure_logging

log = configure_logging("host-simulator", os.environ.get("LOG_LEVEL", "INFO"))

HOST = os.environ.get("SIMULATOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("SIMULATOR_PORT", "9999"))


def main() -> None:
    simulator = HostSimulator(host=HOST, port=PORT)
    simulator.start()
    log.info(f"host-simulator listening on {HOST}:{PORT}")

    stop = threading.Event()

    def _shutdown(signum, _frame):
        # SIGTERM is what OpenShift sends first on a rolling update. Handling
        # it means the listening socket closes cleanly instead of waiting out
        # terminationGracePeriodSeconds and then being SIGKILLed.
        log.info(f"received signal {signum}, shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    stop.wait()
    simulator.stop()
    log.info("host-simulator stopped")


if __name__ == "__main__":
    sys.exit(main())
