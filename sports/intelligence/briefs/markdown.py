from __future__ import annotations

from html import escape

from sports.intelligence.briefs.models import MlbIntelligenceBrief


def _format_probability(label: str, value: float | None, reason: str | None) -> str:
    if value is None:
        return f"{label}: Unavailable" + (f" ({reason})" if reason else "")
    return f"{label}: {value:.3f}"


def _format_difference(value: float | None, reason: str | None) -> str:
    if value is None:
        return "Probability difference: Unavailable" + (
            f" ({reason})" if reason else ""
        )
    return f"Probability difference: {value:+.3f}"


def render_brief(report: MlbIntelligenceBrief) -> str:
    lines: list[str] = ["# MLB Intelligence Brief", "", "## Report Metadata", ""]
    metadata = report.metadata
    lines.extend(
        [
            f"- Report ID: {metadata.report_id}",
            f"- Generated at: {metadata.generated_at}",
            f"- Slate date: {metadata.slate_date}",
            f"- League: {metadata.league}",
            f"- Data freshness: {metadata.data_freshness}",
            f"- Methodology version: {metadata.methodology_version}",
            "",
            "## Slate Summary",
            "",
            f"- Games analyzed: {report.slate_summary.games_analyzed}",
            "- Markets available: "
            + (
                ", ".join(report.slate_summary.markets_available)
                if report.slate_summary.markets_available
                else "Unavailable: no markets were available."
            ),
            "- Books represented: "
            + (
                ", ".join(report.slate_summary.books_represented)
                if report.slate_summary.books_represented
                else "Unavailable: no books were represented."
            ),
            "- Stale or incomplete markets: "
            + (
                "; ".join(report.slate_summary.stale_or_incomplete_markets)
                if report.slate_summary.stale_or_incomplete_markets
                else "None."
            ),
            "- Highest-priority research items: "
            + (
                "; ".join(report.slate_summary.highest_priority_research_items)
                if report.slate_summary.highest_priority_research_items
                else "Unavailable: no MLB events were available for this slate."
            ),
            "",
            "## Game Cards",
            "",
        ]
    )
    if not report.game_cards:
        lines.append("No MLB games were available in the persisted snapshot.")
        lines.append("")
    for card in report.game_cards:
        lines.extend(
            [
                f"### {card.teams}",
                f"- Canonical game ID: {card.canonical_game_id}",
                f"- Scheduled start: {card.scheduled_start}",
                f"- Data-quality status: {card.data_quality_status}",
                f"- Confidence: {card.confidence_label}",
                f"- Research selection: {card.probabilities.research_selection or 'Unavailable'}",
                _format_probability(
                    "- Consensus no-vig market probability",
                    card.probabilities.market_probability,
                    card.probabilities.market_probability_reason,
                ),
                _format_probability(
                    "- Experimental model probability",
                    card.probabilities.model_probability,
                    card.probabilities.model_probability_reason,
                ),
                "- "
                + _format_difference(
                    card.probabilities.probability_difference,
                    card.probabilities.probability_difference_reason,
                ),
                f"- Verdict: {card.verdict.capitalize()}",
                f"- Verdict detail: {card.verdict_detail}",
            ]
        )
        if card.stale_warning:
            lines.append(f"- Stale quote warning: {card.stale_warning}")
        lines.append("- Available sportsbook quotes:")
        if not card.available_sportsbook_quotes:
            lines.append("  Unavailable: no sportsbook quotes were persisted.")
        else:
            for quote in card.available_sportsbook_quotes:
                lines.extend(
                    [
                        f"  - {quote.sportsbook} {quote.selection} {quote.american_price}",
                        f"    - Source/provider: {quote.provider}",
                        f"    - Observed timestamp: {quote.observed_at}",
                        f"    - Provider event ID: {quote.provider_event_id}",
                        f"    - Provider quote ID: {quote.provider_quote_id}",
                        f"    - Source URL: {quote.source_url}",
                    ]
                )
        lines.append("- Evidence references:")
        if card.evidence_references:
            lines.extend(f"  - {item}" for item in card.evidence_references)
        else:
            lines.append("  - Unavailable: no evidence references were persisted.")
        lines.append("- Contradictions:")
        if card.contradictions:
            lines.extend(f"  - {item}" for item in card.contradictions)
        else:
            lines.append("  - None recorded.")
        lines.append("- Risk flags:")
        if card.risk_flags:
            lines.extend(f"  - {item}" for item in card.risk_flags)
        else:
            lines.append("  - None recorded.")
        lines.append("")

    lines.extend(["## Audit And Disclosure", ""])
    lines.append(f"- Generated timestamp: {report.disclosure.generated_timestamp}")
    lines.append("- Audit trail:")
    if report.disclosure.audit_entries:
        for item in report.disclosure.audit_entries:
            lines.extend(
                [
                    f"  - Source/provider: {item.source_provider}",
                    f"    - Observed timestamp: {item.observed_timestamp}",
                    f"    - Canonical game ID: {item.canonical_game_id}",
                    f"    - Provider event ID: {item.provider_event_id}",
                    f"    - Provider quote ID: {item.provider_quote_id}",
                    f"    - Source URL: {item.source_url}",
                ]
            )
    else:
        lines.append("  - Unavailable: no audit entries were available.")
    lines.append("- Model limitations:")
    for item in report.disclosure.model_limitations:
        lines.append(f"  - {item}")
    lines.append(f"- Disclosure: {report.disclosure.no_guarantee_language}")
    lines.append(f"- Disclosure: {report.disclosure.no_automation_language}")
    return "\n".join(lines) + "\n"


def render_brief_html(report: MlbIntelligenceBrief) -> str:
    def item_list(items: tuple[str, ...], empty_text: str) -> str:
        if not items:
            return f"<p>{escape(empty_text)}</p>"
        return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"

    cards_html: list[str] = []
    for card in report.game_cards:
        quotes = "".join(
            (
                "<tr>"
                f"<td>{escape(quote.sportsbook)}</td>"
                f"<td>{escape(quote.selection)}</td>"
                f"<td>{quote.american_price}</td>"
                f"<td>{escape(quote.provider)}</td>"
                f"<td>{escape(quote.observed_at)}</td>"
                f"<td>{escape(quote.provider_event_id)}</td>"
                f"<td>{escape(quote.provider_quote_id)}</td>"
                f'<td><a href="{escape(quote.source_url)}">source</a></td>'
                "</tr>"
            )
            for quote in card.available_sportsbook_quotes
        )
        cards_html.append(
            """
<article class="game-card">
  <h3>{teams}</h3>
  <p><strong>Canonical game ID:</strong> {canonical_id}</p>
  <p><strong>Scheduled start:</strong> {scheduled_start}</p>
  <p><strong>Data-quality status:</strong> {data_quality}</p>
  <p><strong>Confidence:</strong> {confidence}</p>
  <p><strong>Research selection:</strong> {selection}</p>
  <p><strong>Consensus no-vig market probability:</strong> {market_probability}</p>
  <p><strong>Experimental model probability:</strong> {model_probability}</p>
  <p><strong>Probability difference:</strong> {difference}</p>
  <p><strong>Verdict:</strong> {verdict}</p>
  <p><strong>Verdict detail:</strong> {verdict_detail}</p>
  {stale_warning}
  <h4>Available Sportsbook Quotes</h4>
  <table>
    <thead>
      <tr><th>Book</th><th>Selection</th><th>Price</th><th>Provider</th><th>Observed</th><th>Provider Event ID</th><th>Provider Quote ID</th><th>Link</th></tr>
    </thead>
    <tbody>{quotes}</tbody>
  </table>
  <h4>Evidence References</h4>
  {evidence}
  <h4>Contradictions</h4>
  {contradictions}
  <h4>Risk Flags</h4>
  {risks}
</article>
            """.format(
                teams=escape(card.teams),
                canonical_id=escape(card.canonical_game_id),
                scheduled_start=escape(card.scheduled_start),
                data_quality=escape(card.data_quality_status),
                confidence=escape(card.confidence_label),
                selection=escape(
                    card.probabilities.research_selection or "Unavailable"
                ),
                market_probability=escape(
                    f"{card.probabilities.market_probability:.3f}"
                    if card.probabilities.market_probability is not None
                    else "Unavailable"
                ),
                model_probability=escape(
                    f"{card.probabilities.model_probability:.3f}"
                    if card.probabilities.model_probability is not None
                    else "Unavailable"
                ),
                difference=escape(
                    f"{card.probabilities.probability_difference:+.3f}"
                    if card.probabilities.probability_difference is not None
                    else "Unavailable"
                ),
                verdict=escape(card.verdict.capitalize()),
                verdict_detail=escape(card.verdict_detail),
                stale_warning=(
                    f"<p><strong>Stale quote warning:</strong> {escape(card.stale_warning)}</p>"
                    if card.stale_warning
                    else ""
                ),
                quotes=quotes,
                evidence=item_list(
                    card.evidence_references,
                    "Unavailable: no evidence references were persisted.",
                ),
                contradictions=item_list(card.contradictions, "None recorded."),
                risks=item_list(card.risk_flags, "None recorded."),
            )
        )

    if not cards_html:
        cards_html.append(
            "<p>No MLB games were available in the persisted snapshot.</p>"
        )

    audit_entries = "".join(
        (
            "<tr>"
            f"<td>{escape(item.source_provider)}</td>"
            f"<td>{escape(item.observed_timestamp)}</td>"
            f"<td>{escape(item.canonical_game_id)}</td>"
            f"<td>{escape(item.provider_event_id)}</td>"
            f"<td>{escape(item.provider_quote_id)}</td>"
            f'<td><a href="{escape(item.source_url)}">source</a></td>'
            "</tr>"
        )
        for item in report.disclosure.audit_entries
    )

    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MLB Intelligence Brief</title>
  <style>
    :root {{ color-scheme: light; --ink:#152033; --muted:#5a687d; --line:#d7deea; --panel:#ffffff; --soft:#f4f7fb; --accent:#0f5cc0; }}
    body {{ margin:0; font-family: Georgia, 'Times New Roman', serif; color:var(--ink); background:linear-gradient(180deg,#eef3fb 0%,#ffffff 240px); }}
    main {{ max-width: 980px; margin: 0 auto; padding: 40px 24px 64px; }}
    header, section, article {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; box-shadow:0 18px 50px rgba(21,32,51,0.08); }}
    header {{ padding:28px; margin-bottom:24px; }}
    section {{ padding:24px 28px; margin-bottom:24px; }}
    article {{ padding:20px 22px; margin:20px 0; background:var(--soft); }}
    h1,h2,h3,h4 {{ margin:0 0 12px; line-height:1.2; }}
    h1 {{ font-size:2.2rem; }}
    h2 {{ font-size:1.35rem; color:var(--accent); }}
    h3 {{ font-size:1.25rem; }}
    p, li, td, th {{ line-height:1.5; }}
    .meta-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; }}
    .metric {{ padding:14px; border-radius:14px; background:var(--soft); border:1px solid var(--line); }}
    table {{ width:100%; border-collapse:collapse; margin-top:12px; font-size:0.95rem; }}
    th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
    th {{ color:var(--muted); font-weight:600; }}
    a {{ color:var(--accent); }}
    .disclosure {{ color:var(--muted); }}
  </style>
</head>
<body>
  <main>
    <header>
      <p>Professional research brief for manual analyst review.</p>
      <h1>MLB Intelligence Brief</h1>
      <p class="disclosure">This report is research and analysis, not a guarantee of results or outcomes, and no automated execution is authorized or implied.</p>
    </header>
    <section>
      <h2>Report Metadata</h2>
      <div class="meta-grid">
        <div class="metric"><strong>Report ID</strong><br>{report_id}</div>
        <div class="metric"><strong>Generated at</strong><br>{generated_at}</div>
        <div class="metric"><strong>Slate date</strong><br>{slate_date}</div>
        <div class="metric"><strong>League</strong><br>{league}</div>
        <div class="metric"><strong>Data freshness</strong><br>{freshness}</div>
        <div class="metric"><strong>Methodology version</strong><br>{methodology}</div>
      </div>
    </section>
    <section>
      <h2>Slate Summary</h2>
      <p><strong>Games analyzed:</strong> {games_analyzed}</p>
      <p><strong>Markets available:</strong> {markets}</p>
      <p><strong>Books represented:</strong> {books}</p>
      <p><strong>Stale or incomplete markets:</strong> {stale}</p>
      <p><strong>Highest-priority research items:</strong> {priority}</p>
    </section>
    <section>
      <h2>Game Cards</h2>
      {cards}
    </section>
    <section>
      <h2>Audit and Disclosure</h2>
      <p><strong>Generated timestamp:</strong> {audit_generated_at}</p>
      <h3>Audit Trail</h3>
      <table>
        <thead>
          <tr><th>Source/provider</th><th>Observed timestamp</th><th>Canonical game ID</th><th>Provider event ID</th><th>Provider quote ID</th><th>Link</th></tr>
        </thead>
        <tbody>{audit_entries}</tbody>
      </table>
      <h3>Model Limitations</h3>
      {limitations}
      <p class="disclosure"><strong>Disclosure:</strong> {no_guarantee}</p>
      <p class="disclosure"><strong>Disclosure:</strong> {no_automation}</p>
    </section>
  </main>
</body>
</html>
""".format(
        report_id=escape(report.metadata.report_id),
        generated_at=escape(report.metadata.generated_at),
        slate_date=escape(report.metadata.slate_date),
        league=escape(report.metadata.league),
        freshness=escape(report.metadata.data_freshness),
        methodology=escape(report.metadata.methodology_version),
        games_analyzed=report.slate_summary.games_analyzed,
        markets=escape(
            ", ".join(report.slate_summary.markets_available)
            if report.slate_summary.markets_available
            else "Unavailable: no markets were available."
        ),
        books=escape(
            ", ".join(report.slate_summary.books_represented)
            if report.slate_summary.books_represented
            else "Unavailable: no books were represented."
        ),
        stale=escape(
            "; ".join(report.slate_summary.stale_or_incomplete_markets)
            if report.slate_summary.stale_or_incomplete_markets
            else "None."
        ),
        priority=escape(
            "; ".join(report.slate_summary.highest_priority_research_items)
            if report.slate_summary.highest_priority_research_items
            else "Unavailable: no MLB events were available for this slate."
        ),
        cards="".join(cards_html),
        audit_generated_at=escape(report.disclosure.generated_timestamp),
        audit_entries=audit_entries
        or '<tr><td colspan="6">Unavailable: no audit entries were available.</td></tr>',
        limitations=item_list(
            report.disclosure.model_limitations,
            "No model limitations were recorded.",
        ),
        no_guarantee=escape(report.disclosure.no_guarantee_language),
        no_automation=escape(report.disclosure.no_automation_language),
    )


def render_brief_pdf(report: MlbIntelligenceBrief) -> bytes:
    text = render_brief(report)
    lines = [line if line else " " for line in text.splitlines()]
    lines_per_page = 46
    page_chunks = [
        lines[index : index + lines_per_page]
        for index in range(0, len(lines), lines_per_page)
    ] or [[" "]]

    def escape_pdf_text(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: list[bytes] = []
    page_object_numbers: list[int] = []
    object_number = 1
    pages_object_number = 2
    font_object_number = 3
    next_object_number = 4

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [] /Count 0 >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for chunk in page_chunks:
        content_lines = [b"BT", b"/F1 10 Tf", b"50 790 Td", b"14 TL"]
        for line in chunk:
            encoded = escape_pdf_text(line).encode("latin-1", "replace")
            content_lines.append(b"(" + encoded + b") Tj")
            content_lines.append(b"T*")
        content_lines.append(b"ET")
        stream = b"\n".join(content_lines)
        content_object_number = next_object_number
        page_object_number = next_object_number + 1
        next_object_number += 2
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_object_number} 0 R >> >> "
                f"/Contents {content_object_number} 0 R >>"
            ).encode("ascii")
        )
        page_object_numbers.append(page_object_number)

    kids = "[" + " ".join(f"{number} 0 R" for number in page_object_numbers) + "]"
    objects[pages_object_number - 1] = (
        f"<< /Type /Pages /Kids {kids} /Count {len(page_object_numbers)} >>"
    ).encode("ascii")

    pdf_parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    current_offset = len(pdf_parts[0])
    for number, body in enumerate(objects, start=1):
        offsets.append(current_offset)
        obj = f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        pdf_parts.append(obj)
        current_offset += len(obj)
    xref_offset = current_offset
    xref_lines = [f"0 {len(objects) + 1}", "0000000000 65535 f "]
    for offset in offsets[1:]:
        xref_lines.append(f"{offset:010d} 00000 n ")
    xref = ("xref\n" + "\n".join(xref_lines) + "\n").encode("ascii")
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    pdf_parts.extend([xref, trailer])
    return b"".join(pdf_parts)
