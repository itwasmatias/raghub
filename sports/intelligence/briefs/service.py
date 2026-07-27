from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sports.intelligence.briefs.markdown import (
    render_brief,
    render_brief_html,
    render_brief_pdf,
)
from sports.intelligence.briefs.models import (
    BriefAuditEntry,
    BriefDisclosure,
    BriefGameCard,
    BriefMetadata,
    BriefProbabilityView,
    BriefQuoteObservation,
    BriefSummary,
    MlbIntelligenceBrief,
)
from sports.personal.wagering import DataQualityStatus


class MlbIntelligenceBriefService:
    METHODOLOGY_VERSION = "mlb-intelligence-brief-v1"
    LEAGUE = "MLB"

    def __init__(
        self,
        *,
        methodology_version: str | None = None,
        odds_max_age_seconds: int = 600,
        clock=None,
    ) -> None:
        self.methodology_version = methodology_version or self.METHODOLOGY_VERSION
        self.odds_max_age_seconds = odds_max_age_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def build_report(self, snapshot: dict[str, Any]) -> MlbIntelligenceBrief:
        generated_at = str(
            snapshot.get("generated_at")
            or self.clock().astimezone(timezone.utc).isoformat()
        )
        generated_time = self._time(generated_at)
        events = self._sorted_events(snapshot)
        cards = tuple(
            self._build_card(event, generated_time, snapshot) for event in events
        )
        slate_date = (
            events[0].get("start_time", generated_at)[:10]
            if events
            else generated_at[:10]
        )
        freshness = self._freshness(snapshot)
        summary = self._build_summary(cards)
        event_ids = [card.canonical_game_id for card in cards]
        report_payload = json.dumps(
            {
                "generated_at": generated_at,
                "league": self.LEAGUE,
                "slate_date": slate_date,
                "event_ids": event_ids,
                "methodology_version": self.methodology_version,
            },
            sort_keys=True,
        )
        metadata = BriefMetadata(
            report_id=(
                "mlb-brief:v1:"
                f"{hashlib.sha256(report_payload.encode('utf-8')).hexdigest()[:12]}"
            ),
            generated_at=generated_at,
            slate_date=slate_date,
            league=self.LEAGUE,
            data_freshness=freshness,
            methodology_version=self.methodology_version,
        )
        disclosure = self._build_disclosure(cards, generated_at)
        unavailable_reason = (
            "Unavailable: no MLB events were available for this slate."
            if not cards
            else None
        )
        notes = (unavailable_reason,) if unavailable_reason else ()
        return MlbIntelligenceBrief(
            metadata=metadata,
            slate_summary=summary,
            game_cards=cards,
            disclosure=disclosure,
            unavailable_reason=unavailable_reason,
            notes=notes,
        )

    def build_markdown(self, snapshot: dict[str, Any]) -> str:
        return render_brief(self.build_report(snapshot))

    def build_html(self, snapshot: dict[str, Any]) -> str:
        return render_brief_html(self.build_report(snapshot))

    def build_pdf(self, snapshot: dict[str, Any]) -> bytes:
        return render_brief_pdf(self.build_report(snapshot))

    def create_delivery_package(
        self,
        *,
        snapshot: dict[str, Any],
        output_dir: str | Path,
        client_slug: str,
        package_notes: tuple[str, ...] = (),
    ) -> dict[str, Path]:
        report = self.build_report(snapshot)
        package_dir = Path(output_dir) / (
            f"mlb-intelligence-brief-{self._slug(client_slug)}-{report.metadata.slate_date}"
        )
        package_dir.mkdir(parents=True, exist_ok=True)

        markdown_path = package_dir / "brief.md"
        html_path = package_dir / "brief.html"
        pdf_path = package_dir / "brief.pdf"
        manifest_path = package_dir / "manifest.json"
        readme_path = package_dir / "README.txt"

        markdown_path.write_text(render_brief(report), encoding="utf-8")
        html_path.write_text(render_brief_html(report), encoding="utf-8")
        pdf_path.write_bytes(render_brief_pdf(report))
        manifest_path.write_text(
            json.dumps(
                {
                    "client_slug": self._slug(client_slug),
                    "report_id": report.metadata.report_id,
                    "generated_at": report.metadata.generated_at,
                    "slate_date": report.metadata.slate_date,
                    "league": report.metadata.league,
                    "methodology_version": report.metadata.methodology_version,
                    "data_freshness": report.metadata.data_freshness,
                    "games_analyzed": report.slate_summary.games_analyzed,
                    "files": {
                        "markdown": markdown_path.name,
                        "html": html_path.name,
                        "pdf": pdf_path.name,
                        "readme": readme_path.name,
                    },
                    "limitations": list(report.disclosure.model_limitations),
                    "notes": [*list(report.notes), *list(package_notes)],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        readme_path.write_text(
            "\n".join(
                [
                    "MLB Intelligence Brief Delivery Package",
                    "",
                    f"Client: {self._slug(client_slug)}",
                    f"Report ID: {report.metadata.report_id}",
                    f"Generated at: {report.metadata.generated_at}",
                    "",
                    "Files:",
                    f"- {markdown_path.name}: Markdown brief for review and editing.",
                    f"- {html_path.name}: Styled HTML brief for browser delivery.",
                    f"- {pdf_path.name}: PDF export for send-out packaging.",
                    f"- {manifest_path.name}: Machine-readable package metadata.",
                    "",
                    "Disclosure:",
                    report.disclosure.no_guarantee_language,
                    report.disclosure.no_automation_language,
                    *(
                        ["", "Package notes:", *[f"- {item}" for item in package_notes]]
                        if package_notes
                        else []
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "package_dir": package_dir,
            "markdown_path": markdown_path,
            "html_path": html_path,
            "pdf_path": pdf_path,
            "manifest_path": manifest_path,
            "readme_path": readme_path,
        }

    def _sorted_events(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        events = [
            dict(item)
            for item in snapshot.get("events") or []
            if str(item.get("league") or "").upper() == self.LEAGUE
        ]
        return sorted(
            events,
            key=lambda item: (
                self._time(str(item.get("start_time") or self.clock().isoformat())),
                str(item.get("canonical_id") or ""),
            ),
        )

    def _build_card(
        self,
        event: dict[str, Any],
        generated_time: datetime,
        snapshot: dict[str, Any],
    ) -> BriefGameCard:
        quotes = sorted(
            [dict(item) for item in event.get("quotes") or []],
            key=lambda item: (
                str(item.get("observed_at") or ""),
                str(item.get("sportsbook") or ""),
                str(item.get("selection") or ""),
                str(item.get("provider_quote_id") or ""),
            ),
        )
        observations = tuple(
            self._quote_observation(item, generated_time) for item in quotes
        )
        evaluations = [
            dict(item)
            for item in (
                event.get("evaluations") or self._event_evaluations(snapshot, event)
            )
        ]
        primary = self._primary_evaluation(evaluations)
        contradictions = self._contradictions(primary)
        data_quality = self._data_quality_status(event, observations, primary)
        probabilities = self._probabilities(primary)
        verdict, verdict_detail = self._verdict(
            event, observations, primary, data_quality
        )
        stale_warning = self._stale_warning(observations)
        confidence_label = (
            f"{probabilities.confidence:.3f}"
            if probabilities.confidence is not None
            else "Unavailable"
        )
        return BriefGameCard(
            canonical_game_id=str(event.get("canonical_id") or ""),
            teams=(
                f"{event.get('home_team_name', 'Unknown Home')} vs "
                f"{event.get('away_team_name', 'Unknown Away')}"
            ),
            scheduled_start=str(event.get("start_time") or "Unavailable"),
            available_sportsbook_quotes=observations,
            probabilities=probabilities,
            confidence_label=confidence_label,
            data_quality_status=data_quality,
            evidence_references=tuple(
                sorted(str(item) for item in (primary or {}).get("evidence") or [])
            ),
            contradictions=contradictions,
            risk_flags=tuple(
                sorted(str(item) for item in (primary or {}).get("risks") or [])
            ),
            verdict=verdict,
            verdict_detail=verdict_detail,
            stale_warning=stale_warning,
        )

    def _event_evaluations(
        self, snapshot: dict[str, Any], event: dict[str, Any]
    ) -> list[dict[str, Any]]:
        event_id = str(event.get("canonical_id") or "")
        return [
            dict(item)
            for item in snapshot.get("evaluations") or []
            if str(item.get("canonical_event_id") or "") == event_id
        ]

    def _quote_observation(
        self, quote: dict[str, Any], generated_time: datetime
    ) -> BriefQuoteObservation:
        observed_at = str(quote.get("observed_at") or "")
        age_minutes = max(
            0.0,
            (generated_time - self._time(observed_at)).total_seconds() / 60.0,
        )
        return BriefQuoteObservation(
            sportsbook=str(quote.get("sportsbook") or "unknown"),
            selection=str(quote.get("selection") or "unknown"),
            american_price=int(quote.get("american_price") or 0),
            observed_at=observed_at,
            provider=str(quote.get("source") or "unknown"),
            provider_event_id=str(quote.get("provider_event_id") or ""),
            provider_quote_id=str(quote.get("provider_quote_id") or ""),
            source_url=str(quote.get("source_url") or ""),
            stale=age_minutes * 60.0 > self.odds_max_age_seconds,
            age_minutes=round(age_minutes, 1),
        )

    def _primary_evaluation(
        self, evaluations: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not evaluations:
            return None
        return sorted(
            evaluations,
            key=lambda item: (
                0 if item.get("qualified") else 1,
                -abs(float(item.get("edge") or 0.0)),
                str(item.get("selection") or ""),
                str(item.get("status") or ""),
            ),
        )[0]

    def _contradictions(self, evaluation: dict[str, Any] | None) -> tuple[str, ...]:
        if evaluation is None:
            return ()
        diagnostics = dict(evaluation.get("diagnostics") or {})
        values = (
            diagnostics.get("contradictions")
            or diagnostics.get("contradicting_evidence")
            or []
        )
        return tuple(sorted(str(item) for item in values))

    def _data_quality_status(
        self,
        event: dict[str, Any],
        observations: tuple[BriefQuoteObservation, ...],
        evaluation: dict[str, Any] | None,
    ) -> str:
        if not observations:
            return DataQualityStatus.UNAVAILABLE.value
        represented_books = {item.sportsbook for item in observations}
        complete_books = int(event.get("complete_books") or 0)
        if any(item.stale for item in observations):
            return DataQualityStatus.STALE.value
        if complete_books < len(represented_books):
            return DataQualityStatus.DEGRADED.value
        if (
            evaluation is not None
            and float(evaluation.get("data_quality") or 0.0) < 0.75
        ):
            return DataQualityStatus.DEGRADED.value
        return DataQualityStatus.VERIFIED.value

    def _probabilities(self, evaluation: dict[str, Any] | None) -> BriefProbabilityView:
        if evaluation is None:
            return BriefProbabilityView(
                market_probability=None,
                market_probability_reason="No persisted evaluation was available.",
                model_probability=None,
                model_probability_reason="No persisted evaluation was available.",
                probability_difference=None,
                probability_difference_reason="No persisted evaluation was available.",
                confidence=None,
                confidence_reason="No persisted evaluation was available.",
                research_selection=None,
            )
        market_probability = self._float_or_none(evaluation.get("market_probability"))
        model_probability = self._float_or_none(evaluation.get("model_probability"))
        confidence = self._float_or_none(evaluation.get("confidence"))
        if market_probability is None:
            market_reason = "No consensus no-vig market probability was persisted."
        else:
            market_reason = None
        if model_probability is None:
            model_reason = "No experimental model probability was persisted."
        else:
            model_reason = None
        if model_probability is None or market_probability is None:
            difference = None
            difference_reason = (
                "Model and market probabilities were not both available."
            )
        else:
            difference = model_probability - market_probability
            difference_reason = None
        if confidence is None:
            confidence_reason = "No confidence score was persisted."
        else:
            confidence_reason = None
        return BriefProbabilityView(
            market_probability=market_probability,
            market_probability_reason=market_reason,
            model_probability=model_probability,
            model_probability_reason=model_reason,
            probability_difference=difference,
            probability_difference_reason=difference_reason,
            confidence=confidence,
            confidence_reason=confidence_reason,
            research_selection=str(evaluation.get("selection") or "").lower() or None,
        )

    def _verdict(
        self,
        event: dict[str, Any],
        observations: tuple[BriefQuoteObservation, ...],
        evaluation: dict[str, Any] | None,
        data_quality: str,
    ) -> tuple[str, str]:
        if evaluation is not None:
            status = str(evaluation.get("status") or "")
            if not bool(evaluation.get("qualified")) or status.upper() == "NO_BET":
                return (
                    "avoid",
                    f"No-bet status preserved from evaluation: {status or 'NO_BET'}",
                )
            if data_quality in {
                DataQualityStatus.STALE.value,
                DataQualityStatus.DEGRADED.value,
            }:
                return (
                    "monitor",
                    "Qualified angle exists, but stale or degraded market inputs require monitoring.",
                )
            return (
                "investigate",
                "Qualified angle is worth manual research review before any decision.",
            )
        if not observations:
            return "avoid", "No sportsbook observations were available."
        if data_quality in {
            DataQualityStatus.STALE.value,
            DataQualityStatus.DEGRADED.value,
        }:
            return (
                "monitor",
                "Market inputs exist, but freshness or completeness issues require monitoring.",
            )
        return (
            "monitor",
            "Market inputs were available, but no persisted model evaluation was available.",
        )

    def _stale_warning(
        self, observations: tuple[BriefQuoteObservation, ...]
    ) -> str | None:
        stale_rows = [item for item in observations if item.stale]
        if not stale_rows:
            return None
        oldest = max(item.age_minutes for item in stale_rows)
        return f"One or more quotes are stale; oldest observed quote is {oldest:.1f} minutes old."

    def _build_summary(self, cards: tuple[BriefGameCard, ...]) -> BriefSummary:
        markets = sorted({"moneyline/full_game" for _ in cards})
        books = sorted(
            {
                quote.sportsbook
                for card in cards
                for quote in card.available_sportsbook_quotes
            }
        )
        stale_or_incomplete = []
        priority = []
        verdict_rank = {"investigate": 0, "monitor": 1, "avoid": 2}
        for card in cards:
            if card.data_quality_status in {
                DataQualityStatus.STALE.value,
                DataQualityStatus.DEGRADED.value,
                DataQualityStatus.UNAVAILABLE.value,
            }:
                stale_or_incomplete.append(
                    f"{card.canonical_game_id} ({card.data_quality_status})"
                )
            priority.append(
                (
                    verdict_rank.get(card.verdict, 9),
                    card.scheduled_start,
                    card.canonical_game_id,
                    f"{card.canonical_game_id}: {card.verdict}",
                )
            )
        highest_priority = tuple(item[3] for item in sorted(priority))
        return BriefSummary(
            games_analyzed=len(cards),
            markets_available=tuple(markets),
            books_represented=tuple(books),
            stale_or_incomplete_markets=tuple(sorted(stale_or_incomplete)),
            highest_priority_research_items=highest_priority,
        )

    def _build_disclosure(
        self, cards: tuple[BriefGameCard, ...], generated_at: str
    ) -> BriefDisclosure:
        entries = []
        for card in cards:
            for quote in card.available_sportsbook_quotes:
                entries.append(
                    BriefAuditEntry(
                        source_provider=quote.provider,
                        observed_timestamp=quote.observed_at,
                        provider_event_id=quote.provider_event_id,
                        provider_quote_id=quote.provider_quote_id,
                        canonical_game_id=card.canonical_game_id,
                        source_url=quote.source_url,
                    )
                )
        audit_entries = tuple(
            sorted(
                entries,
                key=lambda item: (
                    item.observed_timestamp,
                    item.source_provider,
                    item.provider_quote_id,
                    item.canonical_game_id,
                ),
            )
        )
        return BriefDisclosure(
            audit_entries=audit_entries,
            generated_timestamp=generated_at,
            model_limitations=(
                "Experimental model probabilities are directional research inputs and may be missing for some games.",
                "Consensus no-vig market probabilities depend on available complete sportsbook books and may be unavailable.",
            ),
            no_guarantee_language=(
                "This report is research and analysis for manual review, not a guarantee of results or outcomes."
            ),
            no_automation_language=(
                "This report supports research workflows only; no automated execution is authorized or implied."
            ),
        )

    def _freshness(self, snapshot: dict[str, Any]) -> str:
        feed = dict(snapshot.get("feed") or {})
        freshness = str(feed.get("freshness") or "unavailable")
        last_refresh = feed.get("last_successful_refresh")
        if last_refresh:
            return f"{freshness} (last successful refresh: {last_refresh})"
        return freshness

    @staticmethod
    def _slug(value: str) -> str:
        cleaned = [char.lower() if char.isalnum() else "-" for char in value.strip()]
        joined = "".join(cleaned).strip("-")
        return "-".join(part for part in joined.split("-") if part) or "client"

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        return None if value is None else float(value)

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)
