from __future__ import annotations

from core.consistency_engine import (
    ConsistencyDecision,
    ConsistencyEngine,
    ConsistencyRequest,
)


class _Policy:
    def __init__(self, decision: ConsistencyDecision) -> None:
        self.decision = decision
        self.received: ConsistencyRequest | None = None

    def evaluate(self, request: ConsistencyRequest) -> ConsistencyDecision:
        self.received = request
        return self.decision


def test_consistency_engine_delegates_the_complete_request_to_policy() -> None:
    request = ConsistencyRequest(
        units=(),
        expected_unit_ids=("unit-a",),
        closure_reason="timeout",
        closed_at_ns=10,
    )
    expected = ConsistencyDecision(
        status="incomplete",
        received_unit_ids=(),
        missing_unit_ids=("unit-a",),
        final_state="unknown",
        final_action_level=0,
        final_action="continue",
        confidence=0.0,
        data_quality_score=0.0,
        has_conflict=False,
        conflict_reasons=(),
        degraded=True,
    )
    policy = _Policy(expected)

    actual = ConsistencyEngine(policy).evaluate(request)

    assert actual is expected
    assert policy.received is request

