"""
Risk layer: decides whether a transaction should be attempted at all,
before anything is sent over ISO 8583.

Runtime-order note: this runs BEFORE the security layer, message building,
or correlation, it's a gate checked immediately when a request comes in,
not something bolted on after the fact. If this declines a transaction,
nothing below it in the stack (PIN encryption, message send, ledger) ever
runs.

Three signals, none of them certainty on their own:
  - velocity   -- too many attempts, too fast, on the same card
  - amount     -- unusually large, in absolute terms
  - entry mode, DE 22 from Layer 1; manual key entry is inherently
                  riskier than a chip read, especially combined with a
                  larger amount

Each rule can escalate the outcome from approve -> review -> decline, and
the FINAL outcome is whichever rule pushed hardest, with every triggered
reason collected along the way.

Velocity tracking itself is NOT owned by this class anymore, it's
injected as a VelocityTracker (see cache/velocity_tracker.py), so this
class doesn't need to know or care whether "how many recent attempts" is
answered by a local dict or a shared Redis store. Defaults to the
in-memory tracker so existing code that doesn't pass one keeps working
unchanged.
"""

from dataclasses import dataclass, field

from app.velocity import VelocityTracker, InMemoryVelocityTracker

_SEVERITY = {"approve": 0, "review": 1, "decline": 2}


@dataclass
class RiskDecision:
    outcome: str                        # "approve" | "review" | "decline"
    reasons: list = field(default_factory=list)


class RiskEngine:
    def __init__(
        self,
        velocity_tracker: VelocityTracker = None,
        velocity_window_seconds: float = 60,
        velocity_decline_count: int = 5,
        velocity_review_count: int = 3,
        amount_decline_cents: int = 1_000_000,      # $10,000
        amount_review_cents: int = 200_000,          # $2,000
        manual_entry_review_cents: int = 50_000,     # $500
    ):
        self.velocity_tracker = velocity_tracker or InMemoryVelocityTracker()
        self.velocity_window_seconds = velocity_window_seconds
        self.velocity_decline_count = velocity_decline_count
        self.velocity_review_count = velocity_review_count
        self.amount_decline_cents = amount_decline_cents
        self.amount_review_cents = amount_review_cents
        self.manual_entry_review_cents = manual_entry_review_cents

    def evaluate(self, card_number: str, amount_cents: int, entry_mode: str = "05") -> RiskDecision:
        reasons = []
        outcome = "approve"

        def escalate(new_outcome: str, reason: str):
            nonlocal outcome
            reasons.append(reason)
            if _SEVERITY[new_outcome] > _SEVERITY[outcome]:
                outcome = new_outcome

        recent_count = self.velocity_tracker.record_and_count_recent(card_number, self.velocity_window_seconds)
        if recent_count > self.velocity_decline_count:
            escalate("decline", f"{recent_count} attempts within {self.velocity_window_seconds:.0f}s, velocity limit exceeded")
        elif recent_count > self.velocity_review_count:
            escalate("review", f"{recent_count} attempts within {self.velocity_window_seconds:.0f}s, elevated velocity")

        if amount_cents > self.amount_decline_cents:
            escalate("decline", f"amount {amount_cents} cents exceeds hard limit {self.amount_decline_cents}")
        elif amount_cents > self.amount_review_cents:
            escalate("review", f"amount {amount_cents} cents exceeds review threshold {self.amount_review_cents}")

        if entry_mode == "01" and amount_cents > self.manual_entry_review_cents:
            escalate("review", "manually keyed entry combined with an elevated amount")

        return RiskDecision(outcome=outcome, reasons=reasons)
