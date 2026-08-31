"""Command-line interface for MissionaryX Revenue Bridge v0.1."""

from __future__ import annotations

import argparse
import sys

from revenue_bridge.demo import run_creator_test_drive
from revenue_bridge.github import GitHubInboundNormalizer
from revenue_bridge.inbox import RevenueInbox


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="revenue-bridge",
        description="MissionaryX Revenue Bridge v0.1 — Governed AI Authority Layer",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # test-drive subcommand
    test_drive_parser = subparsers.add_parser(
        "test-drive",
        help="Run the deterministic creator test drive (Steps A-J)",
    )
    test_drive_parser.add_argument("--quiet", action="store_true", help="Quiet output")

    # status subcommand
    status_parser = subparsers.add_parser(
        "status",
        help="Display phone-friendly status for revenue inbox",
    )
    status_parser.add_argument("--fixture", default="claude_code", help="Fixture name to display")

    args = parser.parse_args()

    if args.command == "test-drive" or args.command is None:
        success = run_creator_test_drive(verbose=not getattr(args, "quiet", False))
        sys.exit(0 if success else 1)
    elif args.command == "status":
        inbox = RevenueInbox()
        event = GitHubInboundNormalizer.load_fixture(args.fixture)
        inbox.ingest(event)
        print(inbox.format_phone_status())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
