from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from sports.execution.models import ExecutionEvent


def _payload_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_hash(
    aggregate_id: str,
    event_type: str,
    version: int,
    payload_hash: str,
    previous_event_hash: str | None,
) -> str:
    seed = "|".join(
        [
            aggregate_id,
            event_type,
            str(version),
            payload_hash,
            previous_event_hash or "",
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def append_event(
    events: tuple[ExecutionEvent, ...] | list[ExecutionEvent],
    *,
    aggregate_id: str,
    event_type: str,
    version: int,
    timestamp: str,
    actor: str,
    correlation_id: str,
    payload: dict[str, object],
    causation_id: str | None = None,
    model_version: str | None = None,
    strategy_version: str | None = None,
    risk_policy_version: str | None = None,
) -> tuple[ExecutionEvent, ...]:
    history = tuple(events)
    previous_event_hash = history[-1].event_hash if history else None
    payload_hash = _payload_hash(payload)
    event_hash = _event_hash(
        aggregate_id, event_type, version, payload_hash, previous_event_hash
    )
    new_event = ExecutionEvent(
        aggregate_id=aggregate_id,
        event_type=event_type,
        version=version,
        timestamp=timestamp,
        actor=actor,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
        payload_hash=payload_hash,
        previous_event_hash=previous_event_hash,
        event_hash=event_hash,
        model_version=model_version,
        strategy_version=strategy_version,
        risk_policy_version=risk_policy_version,
    )
    return history + (new_event,)


def validate_event_chain(
    events: tuple[ExecutionEvent, ...] | list[ExecutionEvent],
) -> bool:
    history = tuple(events)
    previous_event_hash = None
    for event in history:
        if event.payload_hash != _payload_hash(event.payload):
            return False
        if event.previous_event_hash != previous_event_hash:
            return False
        expected_hash = _event_hash(
            event.aggregate_id,
            event.event_type,
            event.version,
            event.payload_hash,
            event.previous_event_hash,
        )
        if event.event_hash != expected_hash:
            return False
        previous_event_hash = event.event_hash
    return True


@dataclass(frozen=True, slots=True)
class EventLogChain:
    events: tuple[ExecutionEvent, ...] = ()

    def append(self, **kwargs: object) -> "EventLogChain":
        return replace(self, events=append_event(self.events, **kwargs))

    def validate(self) -> bool:
        return validate_event_chain(self.events)
