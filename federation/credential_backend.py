"""Credential backend abstraction for opaque secret storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol


class CredentialBackendError(Exception):
    """Base error for credential backend operations."""


class CredentialNotFoundError(CredentialBackendError):
    """Raised when a credential cannot be resolved."""


class CredentialBackend(Protocol):
    """Protocol for opaque credential secret backends.

    Implementations can be:
    - In-memory test backends (for testing only)
    - Production vault integrations (future work)
    - Hardware security modules (future work)

    The backend is responsible for:
    - Storing credential material securely
    - Resolving opaque references to credential material
    - Rotating credentials
    - Revoking/deleting credentials

    The durable MissionaryX broker store persists only opaque backend references,
    never raw credential material.
    """

    def store_credential(self, backend_ref: str, credential_material: bytes) -> None:
        """Store credential material under an opaque reference.

        Args:
            backend_ref: Opaque reference identifier
            credential_material: Raw credential bytes

        Raises:
            CredentialBackendError: If storage fails
        """
        ...

    def resolve_credential(self, backend_ref: str) -> bytes:
        """Resolve an opaque reference to credential material.

        Args:
            backend_ref: Opaque reference identifier

        Returns:
            Raw credential bytes

        Raises:
            CredentialNotFoundError: If reference cannot be resolved
            CredentialBackendError: If resolution fails
        """
        ...

    def delete_credential(self, backend_ref: str) -> None:
        """Delete credential material.

        Args:
            backend_ref: Opaque reference identifier

        Raises:
            CredentialNotFoundError: If reference does not exist
            CredentialBackendError: If deletion fails
        """
        ...


class InMemoryCredentialBackend:
    """In-memory credential backend for testing only.

    WARNING: This is NOT a production secret vault.
    - Secrets are stored in memory only
    - Secrets are lost on process restart
    - No encryption at rest
    - No audit logging

    Use only for tests with synthetic credential material.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def store_credential(self, backend_ref: str, credential_material: bytes) -> None:
        """Store credential in memory."""
        if not isinstance(backend_ref, str) or not backend_ref:
            raise ValueError("backend_ref must be a non-empty string")
        if not isinstance(credential_material, bytes) or not credential_material:
            raise ValueError("credential_material must be non-empty bytes")
        self._store[backend_ref] = credential_material

    def resolve_credential(self, backend_ref: str) -> bytes:
        """Resolve credential from memory."""
        if not isinstance(backend_ref, str) or not backend_ref:
            raise ValueError("backend_ref must be a non-empty string")
        try:
            return self._store[backend_ref]
        except KeyError as exc:
            raise CredentialNotFoundError(f"Credential {backend_ref!r} not found") from exc

    def delete_credential(self, backend_ref: str) -> None:
        """Delete credential from memory."""
        if not isinstance(backend_ref, str) or not backend_ref:
            raise ValueError("backend_ref must be a non-empty string")
        try:
            del self._store[backend_ref]
        except KeyError as exc:
            raise CredentialNotFoundError(f"Credential {backend_ref!r} not found") from exc

    def __repr__(self) -> str:
        """Safe repr without exposing secrets."""
        return f"InMemoryCredentialBackend(stored_refs={len(self._store)})"


__all__ = [
    "CredentialBackend",
    "CredentialBackendError",
    "CredentialNotFoundError",
    "InMemoryCredentialBackend",
]
