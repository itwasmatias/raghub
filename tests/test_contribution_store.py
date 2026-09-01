import json
from datetime import date, datetime, timezone

import pytest

from revenue_bridge.capital import CapitalMode, CapitalPolicy
from revenue_bridge.contribution import (
    ContributionKind,
    PaymentObservation,
    PaymentVerificationState,
)
from revenue_bridge.contribution_store import (
    ContributionStoreCorruptionError,
    DurableContributionLedger,
)


NOW = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)


def verified_payment(
    payment_id: str = "pay_1",
    amount: float = 50.0,
) -> PaymentObservation:
    return PaymentObservation(
        payment_id=payment_id,
        opportunity_id="opp_1",
        amount_usd=amount,
        state=PaymentVerificationState.VERIFIED,
        evidence_ref=f"receipt:{payment_id}",
        observed_at=NOW,
        provider_reference=f"provider:{payment_id}",
    )


def test_missing_store_replays_as_empty_ledger(tmp_path):
    store = DurableContributionLedger(tmp_path / "contributions.jsonl")

    snapshot = store.capital_snapshot(as_of_date=date(2026, 8, 31))

    assert snapshot.total_cost_usd == 0.0
    assert snapshot.verified_customer_revenue_usd == 0.0


def test_cost_survives_restart_and_replay(tmp_path):
    path = tmp_path / "contributions.jsonl"

    first = DurableContributionLedger(path)
    first.record_cost(
        entry_id="acq_1",
        opportunity_id="opp_1",
        kind=ContributionKind.ACQUISITION_COST,
        amount_usd=5,
        evidence_ref="receipt:acq",
        occurred_at=NOW,
    )

    restarted = DurableContributionLedger(path)
    snapshot = restarted.capital_snapshot(
        as_of_date=date(2026, 8, 31)
    )

    assert snapshot.acquisition_spend_usd == 5.0
    assert snapshot.today_acquisition_spend_usd == 5.0


def test_verified_payment_survives_restart_and_rebuilds_contribution(tmp_path):
    path = tmp_path / "contributions.jsonl"
    store = DurableContributionLedger(path)

    store.record_cost(
        entry_id="cost_12",
        opportunity_id="opp_1",
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=12,
        evidence_ref="receipt:cost",
        occurred_at=NOW,
    )
    store.record_verified_payment(verified_payment(amount=50))

    restarted = DurableContributionLedger(path)
    snapshot = restarted.capital_snapshot(
        as_of_date=date(2026, 8, 31)
    )

    assert snapshot.total_cost_usd == 12.0
    assert snapshot.verified_customer_revenue_usd == 50.0
    assert snapshot.net_contribution_usd == 38.0
    assert snapshot.unrecovered_loss_usd == 0.0


def test_sixty_dollar_hard_stop_survives_restart(tmp_path):
    path = tmp_path / "contributions.jsonl"
    store = DurableContributionLedger(path)

    store.record_cost(
        entry_id="loss_60",
        opportunity_id="opp_1",
        kind=ContributionKind.FULFILLMENT_COST,
        amount_usd=60,
        evidence_ref="receipt:loss",
        occurred_at=NOW,
    )

    restarted = DurableContributionLedger(path)
    snapshot = restarted.capital_snapshot()

    assert snapshot.unrecovered_loss_usd == 60.0
    assert snapshot.mode(CapitalPolicy()) == CapitalMode.HARD_STOP


def test_duplicate_cost_id_after_restart_is_rejected_without_append(tmp_path):
    path = tmp_path / "contributions.jsonl"
    store = DurableContributionLedger(path)

    store.record_cost(
        entry_id="same",
        opportunity_id="opp_1",
        kind=ContributionKind.MODEL_API_COST,
        amount_usd=1,
        evidence_ref="receipt:api",
        occurred_at=NOW,
    )
    before = path.read_bytes()

    restarted = DurableContributionLedger(path)

    with pytest.raises(ValueError, match="duplicate contribution entry_id"):
        restarted.record_cost(
            entry_id="same",
            opportunity_id="opp_1",
            kind=ContributionKind.MODEL_API_COST,
            amount_usd=1,
            evidence_ref="receipt:api",
            occurred_at=NOW,
        )

    assert path.read_bytes() == before


def test_duplicate_payment_after_restart_is_rejected_without_append(tmp_path):
    path = tmp_path / "contributions.jsonl"
    store = DurableContributionLedger(path)
    observation = verified_payment()

    store.record_verified_payment(observation)
    before = path.read_bytes()

    restarted = DurableContributionLedger(path)

    with pytest.raises(ValueError, match="payment already counted"):
        restarted.record_verified_payment(observation)

    assert path.read_bytes() == before


def test_partial_final_record_fails_closed_on_load(tmp_path):
    path = tmp_path / "contributions.jsonl"
    path.write_bytes(b'{"record_type":"cost"}')

    store = DurableContributionLedger(path)

    with pytest.raises(
        ContributionStoreCorruptionError,
        match="incomplete final record",
    ):
        store.load()


def test_corrupt_history_blocks_new_append_and_preserves_bytes(tmp_path):
    path = tmp_path / "contributions.jsonl"
    corrupt = b'{"record_type":"cost"}'
    path.write_bytes(corrupt)

    store = DurableContributionLedger(path)

    with pytest.raises(ContributionStoreCorruptionError):
        store.record_cost(
            entry_id="new",
            opportunity_id="opp_1",
            kind=ContributionKind.ACQUISITION_COST,
            amount_usd=5,
            evidence_ref="receipt:new",
            occurred_at=NOW,
        )

    assert path.read_bytes() == corrupt


def test_tampered_cost_amount_is_detected_by_fingerprint(tmp_path):
    path = tmp_path / "contributions.jsonl"
    store = DurableContributionLedger(path)

    store.record_cost(
        entry_id="cost_1",
        opportunity_id="opp_1",
        kind=ContributionKind.ACQUISITION_COST,
        amount_usd=5,
        evidence_ref="receipt:cost",
        occurred_at=NOW,
    )

    record = json.loads(path.read_text())
    record["amount_usd"] = 50.0
    path.write_text(json.dumps(record) + "\n")

    with pytest.raises(
        ContributionStoreCorruptionError,
        match="fingerprint does not match",
    ):
        DurableContributionLedger(path).load()


def test_tampered_payment_amount_is_detected_by_fingerprint(tmp_path):
    path = tmp_path / "contributions.jsonl"
    store = DurableContributionLedger(path)

    store.record_verified_payment(verified_payment(amount=50))

    record = json.loads(path.read_text())
    record["payment"]["amount_usd"] = 5000.0
    path.write_text(json.dumps(record) + "\n")

    with pytest.raises(
        ContributionStoreCorruptionError,
        match="fingerprint does not match",
    ):
        DurableContributionLedger(path).load()


def test_unknown_record_type_fails_closed(tmp_path):
    path = tmp_path / "contributions.jsonl"
    path.write_text(
        json.dumps({"record_type": "mystery"}) + "\n"
    )

    with pytest.raises(
        ContributionStoreCorruptionError,
        match="unknown contribution record_type",
    ):
        DurableContributionLedger(path).load()


def test_unverified_payment_is_never_persisted(tmp_path):
    path = tmp_path / "contributions.jsonl"
    store = DurableContributionLedger(path)

    observation = PaymentObservation(
        payment_id="pay_unverified",
        opportunity_id="opp_1",
        amount_usd=50,
        state=PaymentVerificationState.UNVERIFIED,
        evidence_ref="observation:unverified",
        observed_at=NOW,
    )

    with pytest.raises(ValueError, match="requires VERIFIED"):
        store.record_verified_payment(observation)

    assert not path.exists()
