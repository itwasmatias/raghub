"""Windows Display Runtime authorization intake envelope and validation."""

import json
from dataclasses import dataclass
from datetime import datetime

from federation.integrity import (
    authentication_tag,
    authenticates,
    require_integrity_key,
)
from federation.power_action import (
    PowerAction,
    canonical_json,
    normalize_timestamp,
    require_text,
    timestamp,
)
from federation.power_adapter import (
    PowerExecutionAuthorization,
    PowerCorruptionError,
)


_INTAKE_ENVELOPE_DOMAIN = b"raghub.windows-display-intake-envelope.v1"


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PowerCorruptionError("intake envelope has duplicate JSON keys")
        result[key] = value
    return result


def _intake_envelope_payload(values):
    """Extract canonical payload for authentication."""
    return {
        "action": values["action"].value if isinstance(values["action"], PowerAction) else values["action"],
        "adapter_id": values["adapter_id"],
        "authorization_sequence": values["authorization_sequence"],
        "component_id": values["component_id"],
        "controller_authority": values["controller_authority"],
        "envelope_id": values["envelope_id"],
        "expires_at": timestamp(values["expires_at"]) if isinstance(values["expires_at"], datetime) else values["expires_at"],
        "intake_sequence": values["intake_sequence"],
        "integrity_authority": values["integrity_authority"],
        "issued_at": timestamp(values["issued_at"]) if isinstance(values["issued_at"], datetime) else values["issued_at"],
        "policy_version": values["policy_version"],
        "proposal_id": values["proposal_id"],
        "runtime_identity": values["runtime_identity"],
        "schema_version": values["schema_version"],
        "worker_id": values["worker_id"],
    }


@dataclass(slots=True, frozen=True)
class IntakeEnvelope:
    """Immutable intake envelope binding runtime identity and authorization.

    This envelope carries an already-authorized execution request to the
    trusted local Windows runtime. HMAC authentication prevents untrusted
    reconstruction and accidental misuse.

    Security boundary: Malicious Python code already executing in the trusted
    runtime process is outside this authorization boundary. This is not an
    in-process sandbox.
    """

    schema_version: int
    envelope_id: str
    runtime_identity: str
    worker_id: str
    component_id: str
    adapter_id: str
    action: PowerAction
    policy_version: str
    controller_authority: str
    integrity_authority: str
    proposal_id: str
    authorization_sequence: int
    intake_sequence: int
    issued_at: datetime
    expires_at: datetime
    authentication_tag: str
    authorization_record: dict | None = None

    def __post_init__(self):
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise TypeError("schema_version must be an integer")
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")

        for field in (
            "envelope_id",
            "runtime_identity",
            "worker_id",
            "component_id",
            "adapter_id",
            "policy_version",
            "controller_authority",
            "integrity_authority",
            "proposal_id",
        ):
            object.__setattr__(
                self,
                field,
                require_text(getattr(self, field), field),
            )

        # Normalize action to PowerAction enum
        if isinstance(self.action, str):
            object.__setattr__(self, "action", PowerAction(self.action))
        elif not isinstance(self.action, PowerAction):
            raise TypeError("action must be a PowerAction or string")

        for field in ("authorization_sequence", "intake_sequence"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field} must be an integer")
            if value < 1:
                raise ValueError(f"{field} must be positive")

        for field in ("issued_at", "expires_at"):
            value = getattr(self, field)
            # Parse string timestamps first
            if isinstance(value, str):
                from federation.power_action import parse_timestamp
                value = parse_timestamp(value, field)
            object.__setattr__(
                self,
                field,
                normalize_timestamp(value, field),
            )

        if self.expires_at <= self.issued_at:
            raise ValueError("intake envelope must expire after issuance")

        if not isinstance(self.authentication_tag, str):
            raise TypeError("authentication_tag must be a string")

        if self.authorization_record is not None and not isinstance(
            self.authorization_record, dict
        ):
            raise TypeError("authorization_record must be a dict or None")

    def authenticated_payload(self):
        """Return the canonical payload used for authentication."""
        return _intake_envelope_payload(
            {
                field: getattr(self, field)
                for field in (
                    "schema_version",
                    "envelope_id",
                    "runtime_identity",
                    "worker_id",
                    "component_id",
                    "adapter_id",
                    "action",
                    "policy_version",
                    "controller_authority",
                    "integrity_authority",
                    "proposal_id",
                    "authorization_sequence",
                    "intake_sequence",
                    "issued_at",
                    "expires_at",
                )
            }
        )


class IntakeEnvelopeAuthority:
    """Narrow HMAC-SHA256 issuer and verifier for intake envelopes."""

    __slots__ = ("_key",)

    def __init__(self, intake_key):
        self._key = require_integrity_key(intake_key)

    def issue(
        self,
        *,
        schema_version=1,
        envelope_id,
        runtime_identity,
        worker_id,
        component_id,
        adapter_id,
        action,
        policy_version,
        controller_authority,
        integrity_authority,
        proposal_id,
        authorization_sequence,
        intake_sequence,
        issued_at,
        expires_at,
        authorization_record=None,
    ):
        """Issue a new authenticated intake envelope."""
        values = {
            "schema_version": schema_version,
            "envelope_id": envelope_id,
            "runtime_identity": runtime_identity,
            "worker_id": worker_id,
            "component_id": component_id,
            "adapter_id": adapter_id,
            "action": action,
            "policy_version": policy_version,
            "controller_authority": controller_authority,
            "integrity_authority": integrity_authority,
            "proposal_id": proposal_id,
            "authorization_sequence": authorization_sequence,
            "intake_sequence": intake_sequence,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "authorization_record": authorization_record,
        }

        # Validate all fields through IntakeEnvelope constructor (without tag)
        # to ensure consistent validation
        temp_envelope = IntakeEnvelope(**values, authentication_tag="placeholder")

        # Now compute the actual tag
        payload = _intake_envelope_payload(values)
        tag = authentication_tag(
            self._key,
            _INTAKE_ENVELOPE_DOMAIN,
            canonical_json(payload),
        )

        return IntakeEnvelope(
            **values,
            authentication_tag=tag,
        )

    def verify(self, envelope):
        """Verify the HMAC authentication tag of an intake envelope."""
        if type(envelope) is not IntakeEnvelope:
            return False
        return authenticates(
            self._key,
            _INTAKE_ENVELOPE_DOMAIN,
            canonical_json(envelope.authenticated_payload()),
            envelope.authentication_tag,
        )


def parse_intake_envelope(envelope_bytes, integrity_key):
    """Parse and validate an intake envelope from canonical JSON bytes."""
    if not isinstance(envelope_bytes, bytes):
        raise TypeError("envelope_bytes must be bytes")

    try:
        text = envelope_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PowerCorruptionError("intake envelope is not UTF-8") from exc

    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except PowerCorruptionError:
        # Re-raise corruption errors as-is (e.g., duplicate keys)
        raise
    except json.JSONDecodeError as exc:
        raise PowerCorruptionError("intake envelope is malformed JSON") from exc

    if not isinstance(data, dict):
        raise PowerCorruptionError("intake envelope must be a JSON object")

    # Extract and validate required fields
    required_fields = {
        "schema_version",
        "envelope_id",
        "runtime_identity",
        "worker_id",
        "component_id",
        "adapter_id",
        "action",
        "policy_version",
        "controller_authority",
        "integrity_authority",
        "proposal_id",
        "authorization_sequence",
        "intake_sequence",
        "issued_at",
        "expires_at",
        "authentication_tag",
    }

    if not required_fields.issubset(data.keys()):
        missing = required_fields - data.keys()
        raise PowerCorruptionError(
            f"intake envelope missing required fields: {sorted(missing)}"
        )

    unknown_fields = set(data.keys()) - required_fields - {"authorization_record"}
    if unknown_fields:
        raise PowerCorruptionError(
            f"intake envelope has unknown fields: {sorted(unknown_fields)}"
        )

    # Construct envelope
    envelope = IntakeEnvelope(**data)

    # Verify authentication
    authority = IntakeEnvelopeAuthority(intake_key=integrity_key)
    if not authority.verify(envelope):
        raise PowerCorruptionError("intake envelope authentication is invalid")

    return envelope
