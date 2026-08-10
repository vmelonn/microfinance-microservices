"""
mfcommon -- primitives shared by every service in the platform.

What belongs in here: things that MUST be byte-for-byte identical across
services, because two implementations diverging would be a production
incident rather than an inconsistency. The ISO 8583 codec is the clearest
case -- if iso8583-adapter and ace-stub disagree about BCD padding by one
nibble, every message silently corrupts.

What does NOT belong in here: business rules. The risk thresholds live in
risk-service, the double-entry rules live in ledger-service. A shared
library that accumulates business logic is how a microservice split quietly
turns back into a distributed monolith -- every service redeploying because
one service's rule changed.
"""

__version__ = "1.0.0"
