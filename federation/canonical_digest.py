"""Canonical digest computation for MissionaryX operation binding.

This module provides the authoritative canonical digest implementation to ensure
consistent operation fingerprinting across all system components.

Critical invariant: operation_digest MUST be computed identically wherever it
appears to maintain operation binding integrity across the canonical gateway.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def operation_digest(operation_params: dict[str, Any]) -> str:
    """Compute canonical digest of operation parameters.

    This is the AUTHORITATIVE digest implementation for operation binding.

    All operation digest computation MUST use this function to ensure:
    - Identical canonicalization across coordinator, adapter, and gateway
    - No drift between duplicate implementations
    - Consistent operation fingerprinting for authorization binding

    Args:
        operation_params: Dictionary of operation parameters (typically
            {"action": str, "provider_id": str} for Pavilion operations)

    Returns:
        Lowercase SHA-256 hex digest of canonical JSON representation

    Canonical JSON parameters (MUST NOT change):
        - ensure_ascii=False (allow Unicode)
        - sort_keys=True (deterministic field order)
        - separators=(",", ":") (minimal whitespace)
        - allow_nan=False (reject NaN/Infinity)
        - UTF-8 encoding
        - SHA-256 hash
        - Lowercase hexadecimal output
    """
    canonical = json.dumps(
        operation_params,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = ["operation_digest"]
