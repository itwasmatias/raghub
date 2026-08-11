from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .manifest import (
    ArtifactProvenanceError,
    DIGEST_VERIFIED,
    DEFAULT_DIGEST_ALGORITHM,
    create_manifest,
    inspect_manifest,
    verify_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.artifact_provenance")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a canonical provenance manifest")
    create.add_argument("--root", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--algorithm", default=DEFAULT_DIGEST_ALGORITHM)
    create.add_argument("artifacts", nargs="+")

    verify = subparsers.add_parser("verify", help="Verify a manifest against a root")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)

    inspect = subparsers.add_parser("inspect", help="Inspect a manifest without verification")
    inspect.add_argument("--manifest", required=True, type=Path)

    return parser


def _print_create_result(manifest) -> None:
    print(
        f"created {DIGEST_VERIFIED} manifest with {len(manifest.artifacts)} artifacts",
        flush=True,
    )


def _print_verify_result(manifest) -> None:
    print(
        f"verified {DIGEST_VERIFIED} manifest with {len(manifest.artifacts)} artifacts",
        flush=True,
    )


def _print_inspect_result(manifest) -> None:
    for line in manifest.summary_lines():
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            manifest = create_manifest(
                args.root,
                args.output,
                args.artifacts,
                algorithm=args.algorithm,
            )
            _print_create_result(manifest)
            return 0
        if args.command == "verify":
            manifest = verify_manifest(args.root, args.manifest)
            _print_verify_result(manifest)
            return 0
        if args.command == "inspect":
            manifest = inspect_manifest(args.manifest)
            _print_inspect_result(manifest)
            return 0
        parser.error(f"unknown command: {args.command}")
        return 2
    except ArtifactProvenanceError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - safety net
        print(f"unexpected error: {exc}", file=sys.stderr)
        return 1

