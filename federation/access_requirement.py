"""Access requirement model for provider access needed by missions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MAX_TEXT_LENGTH = 255


def _require_text(value: Any, field_name: str, *, max_length: int = _MAX_TEXT_LENGTH) -> str:
    """Validate text field with strict rules."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NULL bytes")
    return value


def _canonicalize_provider_scopes(scopes: Any, field_name: str = "provider_scopes") -> tuple[str, ...]:
    """Canonicalize provider scopes to sorted immutable tuple."""
    if isinstance(scopes, str):
        raise TypeError(f"{field_name} must be an iterable of strings, not a single string")

    try:
        items = list(scopes)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of strings") from exc

    if not items:
        raise ValueError(f"{field_name} must contain at least one scope")

    validated: list[str] = []
    seen: set[str] = set()

    for scope in items:
        scope_text = _require_text(scope, field_name)
        if scope_text in seen:
            raise ValueError(f"{field_name} must not contain duplicates")
        seen.add(scope_text)
        validated.append(scope_text)

    # Canonical ordering: sorted
    return tuple(sorted(validated))


@dataclass(frozen=True, slots=True)
class AccessRequirement:
    """Describes provider access needed by a mission.

    An AccessRequirement binds:
    - ControlDomain (organizational boundary)
    - Provider/service identity
    - Required provider scopes
    - Optional account/subject constraint
    - Mission context

    Provider scopes are canonicalized (sorted, deduplicated, immutable).

    IMPORTANT: Provider OAuth scopes are NOT MissionaryX delegation capabilities.
    They are separate dimensions:
        - Provider scope: e.g., "google.drive.file"
        - MissionaryX capability: e.g., "document.upload"
    """

    domain_id: str
    provider: str
    required_scopes: tuple[str, ...]
    mission_id: str
    account_constraint: str | None = None

    def __post_init__(self) -> None:
        """Validate and canonicalize all fields."""
        object.__setattr__(self, "domain_id", _require_text(self.domain_id, "domain_id"))
        object.__setattr__(self, "provider", _require_text(self.provider, "provider"))
        object.__setattr__(self, "mission_id", _require_text(self.mission_id, "mission_id"))

        # Canonicalize provider scopes
        object.__setattr__(
            self,
            "required_scopes",
            _canonicalize_provider_scopes(self.required_scopes, "required_scopes"),
        )

        # Optional account constraint
        if self.account_constraint is not None:
            object.__setattr__(
                self,
                "account_constraint",
                _require_text(self.account_constraint, "account_constraint"),
            )

    def to_dict(self) -> dict[str, Any]:
        """Return safe serializable representation."""
        return {
            "domain_id": self.domain_id,
            "provider": self.provider,
            "required_scopes": list(self.required_scopes),
            "mission_id": self.mission_id,
            "account_constraint": self.account_constraint,
        }


__all__ = [
    "AccessRequirement",
]
