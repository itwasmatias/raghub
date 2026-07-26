from datetime import datetime, timezone

from sports.execution.events import EventLogChain, append_event, validate_event_chain


def test_risk_override_events_create_a_valid_hash_chain():
    now = datetime.now(timezone.utc).isoformat()
    chain = append_event(
        (),
        aggregate_id="order-1",
        event_type="risk_override",
        version=1,
        timestamp=now,
        actor="operator-1",
        correlation_id="corr-1",
        payload={"reason": "manual review approved"},
        model_version="model-v1",
        strategy_version="strategy-v1",
        risk_policy_version="risk-v1",
    )
    chain = append_event(
        chain,
        aggregate_id="order-1",
        event_type="submission_confirmed",
        version=2,
        timestamp=now,
        actor="operator-1",
        correlation_id="corr-1",
        payload={"receipt": "receipt-1"},
        causation_id="risk-override-1",
        model_version="model-v1",
        strategy_version="strategy-v1",
        risk_policy_version="risk-v1",
    )

    assert validate_event_chain(chain)
    assert EventLogChain(chain).validate()


def test_adapters_remain_separated_and_real_execution_stays_disabled():
    from sports.execution.adapters import (
        FutureAuthorizedTransactionalAdapter,
        ManualRecordingAdapter,
        PracticeExecutionAdapter,
    )
    from sports.execution.models import OrderIntentEnvelope, OrderIntentState

    intent = OrderIntentEnvelope(
        intent_id="intent-1",
        market_id="market-1",
        outcome_id="outcome-1",
        state=OrderIntentState.USER_CONFIRMED,
    )

    practice = PracticeExecutionAdapter()
    manual = ManualRecordingAdapter()
    transactional = FutureAuthorizedTransactionalAdapter(authorized=False)

    practice_result = practice.submit(intent, {"created_at": "now"})
    manual_result = manual.submit(intent, {"created_at": "now"})

    assert practice_result["execution_id"] != manual_result["execution_id"]
    assert practice.fetch_receipt(practice_result["execution_id"]) is not None

    try:
        transactional.submit(intent, {})
    except RuntimeError:
        disabled = True
    else:
        disabled = False

    assert disabled
