"""Operator CLI for MissionaryX Job Application Executor v0.1."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_application.browser import FixtureScenario, SimulatedATSBrowserAdapter
from job_application.candidate import load_candidate_profile, synthetic_test_profile
from job_application.executor import (
    DuplicateApplicationError,
    GateARequiredError,
    GateBRequiredError,
    JobApplicationExecutor,
)
from job_application.ledger import DurableApplicationLedger
from job_application.opportunity import DurableOpportunityStore, ingest_from_text


_DEFAULT_DATA_DIR = Path.home() / ".missionaryx" / "job_applications"


def _make_executor(data_dir: Path, scenario: str = "single_page_success") -> JobApplicationExecutor:
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = DurableApplicationLedger(data_dir / "applications.jsonl")
    store = DurableOpportunityStore(data_dir / "opportunities.jsonl")
    browser = SimulatedATSBrowserAdapter(FixtureScenario(scenario))
    return JobApplicationExecutor(ledger=ledger, opportunity_store=store, browser_adapter=browser)


def cmd_ingest(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    executor = _make_executor(data_dir)

    if args.text_file:
        posting_text = Path(args.text_file).read_text("utf-8")
    else:
        posting_text = args.text or ""
        if not posting_text:
            print("ERROR: provide --text-file or --text", file=sys.stderr)
            return 1

    from job_application.opportunity import RemoteStatus, EmploymentType
    opp = ingest_from_text(
        posting_text=posting_text,
        company=args.company,
        title=args.title,
        location=args.location or "Unknown",
        source_url=args.source_url,
        application_url=args.application_url,
        remote_status=RemoteStatus(args.remote) if args.remote else RemoteStatus.UNKNOWN,
    )
    try:
        record = executor.ingest_opportunity(opp)
        print(f"Ingested opportunity: {opp.job_id}")
        print(f"Application ID: {record.application_id}")
        print(f"State: {record.state.value}")
        return 0
    except DuplicateApplicationError as exc:
        print(f"BLOCKED: {exc}")
        print(f"Existing application: {exc.existing_application_id}")
        return 1


def cmd_qualify(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    executor = _make_executor(data_dir)

    if args.profile:
        profile = load_candidate_profile(Path(args.profile))
    else:
        profile = synthetic_test_profile()
        print("[INFO] Using synthetic test profile — supply --profile for real assessment")

    fit = executor.qualify_and_assess(args.application_id, profile)
    print(f"Fit assessment complete:")
    print(f"  Score: {fit.overall_score}/100")
    print(f"  Recommendation: {fit.recommendation.value}")
    print(f"  Rationale: {fit.fit_rationale}")
    return 0


def cmd_build_packet(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    executor = _make_executor(data_dir)

    if args.profile:
        profile = load_candidate_profile(Path(args.profile))
    else:
        profile = synthetic_test_profile()
        print("[INFO] Using synthetic test profile")

    packet = executor.build_application_packet(args.application_id, profile)
    print(f"Application packet built:")
    print(f"  Packet hash: {packet.packet_hash}")
    print(f"  Known answers: {len(packet.known_answers)}")
    print(f"  User required: {len(packet.user_required_questions)}")
    if packet.user_required_questions:
        print("  Blocking questions:")
        for q in packet.user_required_questions[:5]:
            print(f"    [{q.answer_authority.value}] {q.question_text[:60]}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    executor = _make_executor(data_dir)
    print(executor.format_status(args.application_id))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    ledger = DurableApplicationLedger(data_dir / "applications.jsonl")
    records = ledger.load_all()
    if not records:
        print("No applications found.")
        return 0
    for r in records:
        blocking = "⚠ INDETERMINATE" if r.state.value == "submission_indeterminate" else ""
        print(
            f"  {r.application_id[:12]}  {r.company[:20]:<20}  "
            f"{r.title[:30]:<30}  {r.state.value:<25}  {r.fit_score:>3}/100  {blocking}"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="job-apply",
        description="MissionaryX Job Application Executor v0.1 — Governed Application Automation",
    )
    parser.add_argument(
        "--data-dir",
        default=str(_DEFAULT_DATA_DIR),
        help=f"Data directory for ledger/store (default: {_DEFAULT_DATA_DIR})",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest a job posting")
    p_ingest.add_argument("--company", required=True)
    p_ingest.add_argument("--title", required=True)
    p_ingest.add_argument("--text-file", help="Path to file containing posting text")
    p_ingest.add_argument("--text", help="Posting text inline")
    p_ingest.add_argument("--location", default="Unknown")
    p_ingest.add_argument("--source-url")
    p_ingest.add_argument("--application-url")
    p_ingest.add_argument("--remote", choices=["remote", "hybrid", "on_site", "unknown"])

    # qualify
    p_qualify = subparsers.add_parser("qualify", help="Run fit assessment")
    p_qualify.add_argument("application_id")
    p_qualify.add_argument("--profile", help="Path to candidate profile JSON")

    # build-packet
    p_packet = subparsers.add_parser("build-packet", help="Build application packet")
    p_packet.add_argument("application_id")
    p_packet.add_argument("--profile")

    # review
    p_review = subparsers.add_parser("review", help="Show application status")
    p_review.add_argument("application_id")

    # status
    subparsers.add_parser("status", help="List all applications")

    args = parser.parse_args()

    dispatch = {
        "ingest": cmd_ingest,
        "qualify": cmd_qualify,
        "build-packet": cmd_build_packet,
        "review": cmd_review,
        "status": cmd_status,
    }

    if args.command not in dispatch:
        parser.print_help()
        sys.exit(0)

    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
