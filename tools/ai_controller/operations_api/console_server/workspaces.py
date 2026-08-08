"""Workspace and path governance for Console Server."""

from __future__ import annotations

from pathlib import Path


class WorkspaceRegistry:
    """
    Registry of allowed workspace roots for Console Server actions.

    Provides path validation and canonicalization to prevent:
    - Path traversal attacks
    - Symlink escapes
    - Access to unauthorized directories
    """

    def __init__(self, allowed_roots: dict[str, Path]):
        """
        Initialize workspace registry.

        Args:
            allowed_roots: Dict mapping workspace_id -> absolute Path
        """
        if not isinstance(allowed_roots, dict):
            raise TypeError("allowed_roots must be a dict")

        self._roots: dict[str, Path] = {}
        for workspace_id, root in allowed_roots.items():
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                raise ValueError("workspace_id must be a non-empty string")
            if not isinstance(root, Path):
                raise TypeError("workspace root must be a Path")
            if not root.is_absolute():
                raise ValueError(f"workspace root must be absolute: {root}")

            # Resolve to catch symlinks and normalize
            try:
                resolved = root.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"Cannot resolve workspace root {root}: {exc}") from exc

            # Reject filesystem root and home without subdirectory
            if resolved == resolved.parent:
                raise ValueError("Workspace root cannot be filesystem root")

            self._roots[workspace_id] = resolved

    def list_workspaces(self) -> list[tuple[str, Path]]:
        """Return list of (workspace_id, path) tuples."""
        return sorted(self._roots.items())

    def validate_workspace(self, workspace_id: str) -> Path:
        """
        Validate and return workspace path.

        Args:
            workspace_id: Workspace identifier

        Returns:
            Resolved workspace Path

        Raises:
            ValueError: If workspace_id is invalid or not registered
        """
        if not isinstance(workspace_id, str):
            raise TypeError("workspace_id must be a string")

        workspace_id = workspace_id.strip()
        if not workspace_id:
            raise ValueError("workspace_id cannot be empty")

        if workspace_id not in self._roots:
            raise ValueError(f"Unknown workspace: {workspace_id!r}")

        return self._roots[workspace_id]

    def validate_path(self, workspace_id: str, relative_path: str | None = None) -> Path:
        """
        Validate a path within a workspace.

        Args:
            workspace_id: Workspace identifier
            relative_path: Optional relative path within workspace

        Returns:
            Validated absolute Path

        Raises:
            ValueError: If path escapes workspace or is invalid
        """
        root = self.validate_workspace(workspace_id)

        if relative_path is None or not relative_path.strip():
            return root

        # Check for obvious traversal attempts
        if ".." in Path(relative_path).parts:
            raise ValueError("Path traversal is not allowed")

        # Build and resolve full path
        try:
            full_path = (root / relative_path).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Invalid path: {exc}") from exc

        # Ensure resolved path is still within workspace
        try:
            full_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Path escapes workspace boundary") from exc

        return full_path
