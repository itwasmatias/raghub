"""Operator CLI for MissionaryX Job Scout v0.1.

Program: jobs-scout
Subcommands:
  scout                 - Run full discovery pipeline
  opportunities         - List all scored opportunities
  top                   - Show top-ranked opportunities
  show <job_id>         - Show detailed card for one opportunity
  why <job_id>          - Show scoring rationale
  send-to-application   - Hand opportunity to Job Application Executor
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_application.browser import FixtureScenario, SimulatedATSBrowserAdapter
from job_application.candidate import synthetic_test_profile, load_candidate_profile
from job_application.executor import JobApplicationExecutor
from job_application.ledger import DurableApplicationLedger
from job_application.opportunity import DurableOpportunityStore
from job_scout.feed import DurableScoutFeed
from job_scout.handoff import ExecutorHandoff
from job_scout.models import ScoutState, ScoredOpportunity
from job_scout.pipeline import ScoutConfig, ScoutPipeline, _ARCHETYPE_QUERIES
from job_scout.sources.fixture import FixtureJobSource


_DEFAULT_DATA_DIR = Path.home() / ".missionaryx" / "job_scout"
_DEFAULT_APP_DIR = Path.home() / ".missionaryx" / "job_applications"


def _make_feed(data_dir: Path) -> DurableScoutFeed:
    data_dir.mkdir(parents=True, exist_ok=True)
    return DurableScoutFeed(data_dir / "scout_feed.jsonl")


def _make_executor(app_dir: Path) -> JobApplicationExecutor:
    app_dir.mkdir(parents=True, exist_ok=True)
    return JobApplicationExecutor(
        ledger=DurableApplicationLedger(app_dir / "applications.jsonl"),
        opportunity_store=DurableOpportunityStore(app_dir / "opportunities.jsonl"),
        browser_adapter=SimulatedATSBrowserAdapter(FixtureScenario("single_page_success")),
    )


def _state_badge(state: ScoutState) -> str:
    return {
        ScoutState.STRONG_APPLY: "★ STRONG_APPLY",
        ScoutState.APPLY: "→ APPLY",
        ScoutState.REVIEW: "? REVIEW",
        ScoutState.STRETCH: "~ STRETCH",
        ScoutState.SKIP: "✗ SKIP",
        ScoutState.STALE: "⏱ STALE",
        ScoutState.HANDED_TO_EXECUTOR: "↗ HANDED_TO_EXECUTOR",
        ScoutState.DUPLICATE: "= DUPLICATE",
    }.get(state, state.value)


def cmd_scout(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    app_dir = Path(args.app_dir)
    feed = _make_feed(data_dir)

    if args.profile:
        profile = load_candidate_profile(Path(args.profile))
    else:
        profile = synthetic_test_profile()
        print("[INFO] Using synthetic test profile — supply --profile for real assessment")

    sources = [FixtureJobSource()]
    if not args.fixture_only:
        try:
            from job_scout.sources.web import RemoteOKJobSource
            sources.append(RemoteOKJobSource())
        except Exception as exc:
            print(f"[WARN] Could not load RemoteOK source: {exc}")

    config = ScoutConfig(
        rootstock_similarity_threshold=args.similarity_threshold,
        candidate_fit_threshold=args.fit_threshold,
        max_results=args.max_results,
    )

    pipeline = ScoutPipeline(sources=sources, feed=feed, profile=profile, config=config)
    report = pipeline.run(queries=_ARCHETYPE_QUERIES)

    print(f"\nSCOUT RUN COMPLETE")
    print(f"  Sources: {', '.join(report.sources_used)}")
    print(f"  Queries: {len(report.queries_used)}")
    print(f"  Raw fetched: {report.raw_fetched}")
    print(f"  After dedup: {report.after_dedup}")
    print(f"  Above threshold: {report.above_threshold}")
    print(f"  Rejected (hard blocker): {report.rejected_hard_blockers}")
    print(f"  Rejected (below threshold): {report.rejected_below_threshold}")
    print(f"  Persisted: {report.persisted}")
    print(f"  Live discovery: {report.live_discovery_status}")

    if report.errors:
        print(f"\nErrors ({len(report.errors)}):")
        for e in report.errors:
            print(f"  - {e}")

    if report.top_opportunities:
        print(f"\nTOP OPPORTUNITIES:")
        for i, opp in enumerate(report.top_opportunities[:5], 1):
            print(
                f"  {i}. {opp.title} @ {opp.company}\n"
                f"     Rootstock: {opp.rootstock_similarity.total}/100  "
                f"Fit: {opp.candidate_fit.total}/100  "
                f"{opp.match_classification.value}  {_state_badge(opp.scout_state)}\n"
                f"     ID: {opp.job_id}"
            )
    return 0


def cmd_opportunities(args: argparse.Namespace) -> int:
    feed = _make_feed(Path(args.data_dir))
    opps = feed.load_all()
    if not opps:
        print("No opportunities in feed. Run: jobs-scout scout")
        return 0
    print(f"{'ID':16} {'COMPANY':22} {'TITLE':32} {'SIM':4} {'FIT':4} {'STATE':20}")
    print("-" * 100)
    for opp in sorted(opps, key=lambda o: o.score_key, reverse=True):
        print(
            f"{opp.job_id[:14]:<16} {opp.company[:20]:<22} {opp.title[:30]:<32} "
            f"{opp.rootstock_similarity.total:>4} {opp.candidate_fit.total:>4} "
            f"{_state_badge(opp.scout_state)}"
        )
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    feed = _make_feed(Path(args.data_dir))
    opps = feed.load_all()
    if not opps:
        print("No opportunities in feed. Run: jobs-scout scout")
        return 0

    active = [o for o in opps if o.scout_state not in (ScoutState.SKIP, ScoutState.STALE, ScoutState.DUPLICATE)]
    ranked = sorted(active, key=lambda o: o.score_key, reverse=True)[: args.n]

    print(f"\nTOP ROOTSTOCK-STYLE OPPORTUNITIES\n{'=' * 50}")
    for i, opp in enumerate(ranked, 1):
        print(f"\n{i}. {opp.title}")
        print(f"   Company: {opp.company}")
        print(f"   Location: {opp.opportunity.location} | {opp.opportunity.remote_status.value}")
        print(f"   Rootstock similarity: {opp.rootstock_similarity.total}/100")
        print(f"   Candidate fit:        {opp.candidate_fit.total}/100")
        print(f"   Classification:       {opp.match_classification.value}")
        print(f"   State:                {_state_badge(opp.scout_state)}")
        print(f"   Availability:         {opp.availability_status.value}")
        print(f"   ID: {opp.job_id}")
        if opp.match_result.primary_gap:
            print(f"   Primary gap: {opp.match_result.primary_gap}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    feed = _make_feed(Path(args.data_dir))
    scored = feed.get(args.job_id)
    if scored is None:
        print(f"Job {args.job_id!r} not found in feed.")
        return 1

    opp = scored.opportunity
    sim = scored.rootstock_similarity
    fit = scored.candidate_fit

    print(f"\nCOMPANY: {opp.company}")
    print(f"ROLE: {opp.title}")
    print(f"LOCATION: {opp.location} | {opp.remote_status.value}")
    if opp.salary_text:
        print(f"SALARY: {opp.salary_text}")
    print(f"\nRootstock similarity: {sim.total}/100")
    print(f"Candidate fit:        {fit.total}/100")
    print(f"\nClassification:\n{scored.match_classification.value}")
    print(f"\nWhy it matches:")
    print(f"  {scored.match_result.rationale}")

    print(f"\nStrong creator evidence:")
    for ev in fit.best_evidence_to_show:
        print(f"  - {ev}")

    if scored.match_result.primary_gap:
        print(f"\nPrimary gap:\n  {scored.match_result.primary_gap}")
        if scored.match_result.gap_classification:
            print(f"  Gap classification: {scored.match_result.gap_classification}")

    hard_blockers = scored.hard_blockers
    if hard_blockers:
        print(f"\nHard blockers:")
        for hb in hard_blockers:
            print(f"  ⚠ {hb}")

    print(f"\nSource: {opp.source}")
    if opp.source_url:
        print(f"Source URL: {opp.source_url}")
    if opp.application_url:
        print(f"Apply URL: {opp.application_url}")
    print(f"\nState: {_state_badge(scored.scout_state)}")
    print(f"Availability: {scored.availability_status.value}")
    if scored.executor_application_id:
        print(f"Executor application ID: {scored.executor_application_id}")

    return 0


def cmd_why(args: argparse.Namespace) -> int:
    feed = _make_feed(Path(args.data_dir))
    scored = feed.get(args.job_id)
    if scored is None:
        print(f"Job {args.job_id!r} not found in feed.")
        return 1

    sim = scored.rootstock_similarity
    fit = scored.candidate_fit

    print(f"\nSCORING RATIONALE — {scored.title} @ {scored.company}")
    print(f"{'=' * 60}")

    print(f"\nROOTSTOCK SIMILARITY ({sim.total}/100)")
    for dim in sim.dimensions():
        hits = ", ".join(dim.matched_signals[:3]) if dim.matched_signals else "none"
        print(f"  {dim.name:<40} {dim.score:>3}/100  hits: {hits}")

    print(f"\nCANDIDATE FIT ({fit.total}/100)")
    for dim in fit._all_dims():
        ev = dim.evidence[0][:50] if dim.evidence else (dim.gaps[0][:50] if dim.gaps else "")
        blocker = " [HARD BLOCKER]" if dim.is_hard_blocker else ""
        print(f"  {dim.name:<40} {dim.score:>3}/100  {ev}{blocker}")

    print(f"\nCLASSIFICATION: {scored.match_classification.value}")
    print(f"  {scored.match_result.rationale}")

    print(f"\nBEST EVIDENCE TO SHOW:")
    for ev in fit.best_evidence_to_show:
        print(f"  - {ev}")

    reqs = scored.requirement_classifications
    hard = [r for r in reqs if r.classification.value == "hard_blocker"]
    preferred = [r for r in reqs if r.classification.value == "preferred_requirement"]
    if hard:
        print(f"\nHARD BLOCKERS:")
        for r in hard:
            print(f"  ⚠ {r.text}: {r.reason}")
    if preferred:
        print(f"\nPREFERRED (not blocking):")
        for r in preferred[:5]:
            print(f"  ○ {r.text}")

    return 0


def cmd_send_to_application(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    app_dir = Path(args.app_dir)
    feed = _make_feed(data_dir)
    scored = feed.get(args.job_id)
    if scored is None:
        print(f"Job {args.job_id!r} not found in feed.")
        return 1

    executor = _make_executor(app_dir)
    handoff = ExecutorHandoff(executor=executor, feed=feed)
    result = handoff.hand_to_executor(scored)

    if result.success:
        print(f"Handed to executor successfully.")
        print(f"  Job ID: {result.job_id}")
        print(f"  Application ID: {result.application_id}")
        print(f"  Next step: job-apply qualify {result.application_id}")
    elif result.was_duplicate:
        print(f"Application already exists: {result.existing_application_id}")
        print(f"  Run: job-apply review {result.existing_application_id}")
        return 1
    else:
        print(f"Handoff failed: {result.error}")
        return 1

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jobs-scout",
        description="MissionaryX Job Scout v0.1 — Rootstock-style niche opportunity discovery",
    )
    parser.add_argument(
        "--data-dir",
        default=str(_DEFAULT_DATA_DIR),
        help=f"Scout data directory (default: {_DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--app-dir",
        default=str(_DEFAULT_APP_DIR),
        help=f"Job Application Executor data directory (default: {_DEFAULT_APP_DIR})",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # scout
    p_scout = subparsers.add_parser("scout", help="Run full discovery pipeline")
    p_scout.add_argument("--profile", help="Path to candidate profile JSON")
    p_scout.add_argument("--fixture-only", action="store_true", help="Use fixtures only (no live sources)")
    p_scout.add_argument("--similarity-threshold", type=int, default=55)
    p_scout.add_argument("--fit-threshold", type=int, default=45)
    p_scout.add_argument("--max-results", type=int, default=20)

    # opportunities
    subparsers.add_parser("opportunities", help="List all scored opportunities")

    # top
    p_top = subparsers.add_parser("top", help="Show top-ranked opportunities")
    p_top.add_argument("--n", type=int, default=5, help="Number of results to show")

    # show
    p_show = subparsers.add_parser("show", help="Show detailed card for one opportunity")
    p_show.add_argument("job_id")

    # why
    p_why = subparsers.add_parser("why", help="Show scoring rationale for one opportunity")
    p_why.add_argument("job_id")

    # send-to-application
    p_send = subparsers.add_parser("send-to-application", help="Hand opportunity to Job Application Executor")
    p_send.add_argument("job_id")

    args = parser.parse_args()

    dispatch = {
        "scout": cmd_scout,
        "opportunities": cmd_opportunities,
        "top": cmd_top,
        "show": cmd_show,
        "why": cmd_why,
        "send-to-application": cmd_send_to_application,
    }

    if args.command not in dispatch:
        parser.print_help()
        sys.exit(0)

    sys.exit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
