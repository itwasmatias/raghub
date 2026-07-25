from __future__ import annotations

from html import escape
from textwrap import dedent
from typing import Any


def render_situation_room(snapshot: dict[str, Any]) -> str:
    alerts = snapshot.get("alerts") or []
    graph_summary = snapshot.get("graph_summary") or {}
    macro_regime = snapshot.get("macro_regime") or {}
    investigations = snapshot.get("investigations") or []
    opportunities = snapshot.get("opportunities") or []
    risk_profiles = snapshot.get("risk_profiles") or []
    source_status = snapshot.get("source_status") or []
    forecast_tournament = snapshot.get("forecast_tournament") or []
    user_impacts = snapshot.get("user_impact_conclusions") or []

    alert_cards = "".join(_render_alert_card(alert) for alert in alerts)
    opportunity_cards = "".join(
        _render_opportunity_card(item) for item in opportunities[:4]
    )
    risk_cards = "".join(_render_risk_profile(item) for item in risk_profiles[:4])
    investigation_cards = "".join(
        _render_investigation(item) for item in investigations[:3]
    )
    source_cards = "".join(_render_source_status(item) for item in source_status)
    forecast_rows = "".join(_render_forecast_row(item) for item in forecast_tournament)
    government_rows = "".join(
        _render_government_action(item)
        for item in (snapshot.get("government_actions") or [])[:5]
    )
    journal_rows = "".join(
        _render_journal_row(item)
        for item in (snapshot.get("decision_journal") or [])[:5]
    )
    impact_cards = "".join(_render_user_impact(item) for item in user_impacts)

    return dedent(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>RAGHub Global Intelligence Situation Room</title>
            <style>
                :root {{
                    color-scheme: dark;
                    --bg: #07111b;
                    --panel: rgba(13, 24, 39, 0.95);
                    --panel-alt: rgba(15, 30, 50, 0.95);
                    --border: rgba(136, 160, 196, 0.18);
                    --text: #edf5ff;
                    --muted: #93a9c9;
                    --green: #31d98c;
                    --amber: #f4c451;
                    --red: #ff7e87;
                    --blue: #63b8ff;
                    --shadow: 0 22px 50px rgba(0, 0, 0, 0.32);
                }}
                * {{ box-sizing: border-box; }}
                body {{
                    margin: 0;
                    font-family: "Segoe UI", Arial, sans-serif;
                    color: var(--text);
                    background:
                        radial-gradient(circle at top left, rgba(99, 184, 255, 0.12), transparent 25%),
                        radial-gradient(circle at top right, rgba(49, 217, 140, 0.12), transparent 24%),
                        linear-gradient(180deg, #05101a, #091421 45%, #091522 100%);
                }}
                a {{ color: #a8d6ff; text-decoration: none; }}
                .page {{ max-width: 1500px; margin: 0 auto; padding: 22px; }}
                .topbar {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 18px; flex-wrap: wrap; }}
                .brand h1 {{ margin: 0; font-size: 2.2rem; }}
                .brand p {{ margin: 6px 0 0; color: var(--muted); max-width: 860px; line-height: 1.55; }}
                .chip {{ display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 999px; border: 1px solid var(--border); background: rgba(16, 28, 44, 0.92); font-weight: 700; }}
                .grid {{ display: grid; gap: 16px; }}
                .hero {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 16px; }}
                .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 22px; box-shadow: var(--shadow); padding: 18px; }}
                .panel h2 {{ margin: 0 0 12px; font-size: 1rem; letter-spacing: 0.06em; text-transform: uppercase; color: #d8e7ff; }}
                .metric-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
                .metric {{ padding: 14px; border-radius: 18px; border: 1px solid var(--border); background: rgba(17, 31, 50, 0.92); }}
                .metric .label {{ color: var(--muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.08em; }}
                .metric .value {{ font-size: 2rem; font-weight: 800; margin-top: 8px; }}
                .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
                .three-col {{ display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 16px; }}
                .four-col {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }}
                .alert-list, .stack {{ display: grid; gap: 12px; }}
                .alert-card, .stack-card {{ padding: 16px; border-radius: 18px; border: 1px solid var(--border); background: rgba(15, 28, 46, 0.92); }}
                .severity {{ display: inline-flex; padding: 5px 9px; border-radius: 999px; font-size: 0.76rem; font-weight: 800; text-transform: uppercase; }}
                .severity.high {{ background: rgba(255, 126, 135, 0.14); color: #ffc1c5; }}
                .severity.medium {{ background: rgba(244, 196, 81, 0.16); color: #ffe2a4; }}
                .severity.moderate {{ background: rgba(99, 184, 255, 0.16); color: #baddff; }}
                .alert-card h3, .stack-card h3 {{ margin: 10px 0 8px; font-size: 1.15rem; }}
                .qa {{ display: grid; gap: 8px; margin-top: 12px; }}
                .qa-item {{ padding: 10px 12px; border-radius: 14px; background: rgba(11, 22, 38, 0.88); border: 1px solid rgba(136, 160, 196, 0.12); }}
                .qa-item strong {{ display: block; margin-bottom: 4px; color: #dce9ff; }}
                .meta {{ color: var(--muted); font-size: 0.9rem; }}
                ul {{ margin: 8px 0 0 18px; padding: 0; }}
                li {{ margin-bottom: 4px; }}
                .table {{ display: grid; gap: 10px; }}
                .row {{ display: grid; grid-template-columns: 1.3fr 0.9fr 1fr; gap: 10px; padding: 10px 0; border-bottom: 1px solid rgba(136, 160, 196, 0.12); }}
                .row:last-child {{ border-bottom: none; }}
                .risk-dimension {{ margin-top: 8px; }}
                .risk-dimension strong {{ display: inline-block; min-width: 180px; }}
                .status-live {{ color: var(--green); }}
                .status-partial, .status-unconfigured {{ color: var(--amber); }}
                .status-degraded {{ color: var(--red); }}
                .impact-chain {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }}
                .impact-conclusion {{ border-left: 3px solid var(--green); padding-left: 12px; }}
                @media (max-width: 1080px) {{
                    .hero, .three-col, .four-col, .two-col, .metric-grid, .impact-chain {{ grid-template-columns: 1fr; }}
                }}
            </style>
        </head>
        <body>
            <main class="page grid">
                <div class="topbar">
                    <div class="brand">
                        <div class="chip">Flagship Feature · Global Intelligence Situation Room</div>
                        <h1>World Intelligence Graph and Government-to-Market Impact Engine</h1>
                        <p>
                            This surface turns public data into explainable alerts, exposure graphs, risk profiles, and investigation threads.
                            It is powered live by FRED, SEC EDGAR, USAspending, Treasury Fiscal Data, World Bank, optional Congress.gov, and GDELT when rate limits allow.
                        </p>
                    </div>
                    <div class="chip">Generated {escape(str(snapshot.get("generated_at") or ""))}</div>
                </div>

                <section class="hero">
                    <div class="panel">
                        <h2>Situation Overview</h2>
                        <div class="metric-grid">
                            <div class="metric"><div class="label">Alerts</div><div class="value">{len(alerts)}</div></div>
                            <div class="metric"><div class="label">Graph Nodes</div><div class="value">{int(graph_summary.get("node_count") or 0)}</div></div>
                            <div class="metric"><div class="label">Graph Edges</div><div class="value">{int(graph_summary.get("edge_count") or 0)}</div></div>
                            <div class="metric"><div class="label">Macro Regime</div><div class="value">{escape(str(macro_regime.get("regime") or "Unknown"))}</div></div>
                        </div>
                    </div>
                    <div class="panel">
                        <h2>Macro Regime</h2>
                        <h3>{escape(str(macro_regime.get("regime") or "Unavailable"))}</h3>
                        <p>{escape(str(macro_regime.get("summary") or "No macro regime summary available."))}</p>
                        <div class="stack">
                            <div class="stack-card"><strong>CPI YoY</strong><div class="meta">{escape(str((macro_regime.get("indicators") or {}).get("cpi_yoy", "n/a")))}%</div></div>
                            <div class="stack-card"><strong>Unemployment Change</strong><div class="meta">{escape(str((macro_regime.get("indicators") or {}).get("unemployment_change_yoy", "n/a")))} pts</div></div>
                            <div class="stack-card"><strong>Fed Funds</strong><div class="meta">{escape(str((macro_regime.get("indicators") or {}).get("fed_funds", "n/a")))}%</div></div>
                        </div>
                    </div>
                </section>

                <section class="panel">
                    <h2>What These Events Mean for You</h2>
                    <p class="meta">Evidence-linked decision implications for monitoring and research, not personalized financial advice.</p>
                    <div class="stack">{impact_cards or '<div class="stack-card">No evidence-backed user impact conclusions are available.</div>'}</div>
                </section>

                <section class="three-col">
                    <div class="panel"><h2>Emerging Global Risks and Forecasts</h2><div class="alert-list">{alert_cards or '<div class="alert-card">No alerts available.</div>'}</div></div>
                    <div class="panel"><h2>Government Actions and Impact Chains</h2><div class="table">{government_rows or '<div class="row"><div>No government actions available.</div><div></div><div></div></div>'}</div></div>
                    <div class="panel"><h2>Opportunities Requiring Attention</h2><div class="stack">{opportunity_cards or '<div class="stack-card">No opportunities available.</div>'}</div></div>
                </section>

                <section class="two-col">
                    <div class="panel"><h2>Explainable Fragility Scores</h2><div class="stack">{risk_cards or '<div class="stack-card">No risk profiles available.</div>'}</div></div>
                    <div class="panel"><h2>Autonomous Investigations</h2><div class="stack">{investigation_cards or '<div class="stack-card">No active investigations available.</div>'}</div></div>
                </section>

                <section class="two-col">
                    <div class="panel"><h2>Forecast Tournament</h2><div class="table">{forecast_rows}</div></div>
                    <div class="panel"><h2>Prediction and Decision Journal</h2><div class="table">{journal_rows}</div></div>
                </section>

                <section class="two-col">
                    <div class="panel"><h2>World Intelligence Graph Summary</h2><div class="stack-card"><strong>Entity Types</strong><div class="meta">{escape(", ".join(graph_summary.get("entity_types") or []))}</div></div><p class="meta">The graph connects alerts, sources, sectors, companies, countries, opportunities, and risk dimensions so the same knowledge layer can be shared across future RAGHub domains.</p></div>
                    <div class="panel"><h2>Source Health</h2><div class="stack">{source_cards}</div></div>
                </section>
            </main>
        </body>
        </html>
        """
    )


def _render_user_impact(item: dict[str, Any]) -> str:
    evidence = ", ".join(str(value) for value in item.get("evidence_titles") or [])
    invalidators = "".join(
        f"<li>{escape(str(value))}</li>"
        for value in item.get("invalidation_conditions") or []
    )
    return f"""
    <article class="stack-card">
        <span class="severity {escape(str(item.get("impact_level") or "medium"))}">
            {escape(str(item.get("impact_level") or "medium"))} impact
        </span>
        <h3>{escape(str(item.get("title") or "Impact conclusion"))}</h3>
        <div class="meta">Confidence {int(float(item.get("confidence") or 0) * 100)}% · Horizon {escape(str(item.get("forecast_horizon") or "unknown"))}</div>
        <div class="impact-chain">
            <div class="qa-item"><strong>Observed trigger</strong>{escape(str(item.get("observed_trigger") or ""))}</div>
            <div class="qa-item"><strong>Direct effect</strong>{escape(str(item.get("direct_effect") or ""))}</div>
            <div class="qa-item"><strong>Possible next effect</strong>{escape(str(item.get("second_order_effect") or ""))}</div>
        </div>
        <div class="impact-conclusion">
            <strong>How this affects your decisions</strong>
            <p>{escape(str(item.get("user_implication") or ""))}</p>
            <strong>Recommended next action</strong>
            <p>{escape(str(item.get("recommended_action") or ""))}</p>
        </div>
        <div class="meta">Evidence: {escape(evidence or "No evidence title supplied")}</div>
        <div class="qa-item"><strong>Reconsider if</strong><ul>{invalidators or "<li>No invalidation condition supplied.</li>"}</ul></div>
    </article>
    """


def _render_alert_card(alert: dict[str, Any]) -> str:
    evidence_items = "".join(
        f'<li><a href="{escape(str(item.get("url") or "#"))}" target="_blank" rel="noreferrer">{escape(str(item.get("title") or "Evidence"))}</a> · {escape(str(item.get("summary") or ""))}</li>'
        if item.get("url")
        else f"<li>{escape(str(item.get('title') or 'Evidence'))} · {escape(str(item.get('summary') or ''))}</li>"
        for item in alert.get("evidence_supports") or []
    )
    invalidation = "".join(
        f"<li>{escape(str(item))}</li>"
        for item in alert.get("invalidation_conditions") or []
    )
    exposed = ", ".join(str(item) for item in alert.get("who_is_exposed") or [])
    severity = escape(str(alert.get("severity") or "moderate"))
    return f"""
    <article class="alert-card">
        <span class="severity {severity}">{severity}</span>
        <h3>{escape(str(alert.get("title") or "Alert"))}</h3>
        <div class="meta">Confidence {int(float(alert.get("confidence") or 0) * 100)}% · Horizon {escape(str(alert.get("forecast_horizon") or ""))}</div>
        <div class="qa">
            <div class="qa-item"><strong>What happened?</strong>{escape(str(alert.get("what_happened") or ""))}</div>
            <div class="qa-item"><strong>Why does it matter?</strong>{escape(str(alert.get("why_it_matters") or ""))}</div>
            <div class="qa-item"><strong>Who is exposed?</strong>{escape(exposed)}</div>
            <div class="qa-item"><strong>What could happen next?</strong>{escape(str(alert.get("what_could_happen_next") or ""))}</div>
            <div class="qa-item"><strong>What evidence supports this?</strong><ul>{evidence_items or "<li>No evidence listed.</li>"}</ul></div>
            <div class="qa-item"><strong>What would invalidate the forecast?</strong><ul>{invalidation or "<li>No invalidation conditions listed.</li>"}</ul></div>
        </div>
    </article>
    """


def _render_opportunity_card(item: dict[str, Any]) -> str:
    amount = float(item.get("amount") or 0.0)
    return f"""
    <div class="stack-card">
        <h3>{escape(str(item.get("title") or "Opportunity"))}</h3>
        <div class="meta">Tracked amount ${amount / 1_000_000_000:.1f}B</div>
        <p>{escape(str(item.get("why") or ""))}</p>
        <p class="meta">Next: {escape(str(item.get("next") or ""))}</p>
    </div>
    """


def _render_risk_profile(item: dict[str, Any]) -> str:
    dimensions = "".join(
        f'<div class="risk-dimension"><strong>{escape(str(dimension.get("label") or "Dimension"))}</strong> {escape(str(dimension.get("status") or ""))} · {escape(str(dimension.get("score") or 0))}/100<br><span class="meta">{escape(str(dimension.get("rationale") or ""))}</span></div>'
        for dimension in item.get("dimensions") or []
    )
    return f"""
    <div class="stack-card">
        <h3>{escape(str(item.get("subject") or "Subject"))}</h3>
        <div class="meta">{escape(str(item.get("summary") or ""))} · Confidence {int(float(item.get("confidence") or 0) * 100)}%</div>
        {dimensions}
    </div>
    """


def _render_investigation(item: dict[str, Any]) -> str:
    entities = ", ".join(
        str(entity) for entity in item.get("supporting_entities") or []
    )
    next_steps = "".join(
        f"<li>{escape(str(step))}</li>" for step in item.get("next_steps") or []
    )
    return f"""
    <div class="stack-card">
        <h3>{escape(str(item.get("title") or "Investigation"))}</h3>
        <div class="meta">Status {escape(str(item.get("status") or "active"))} · Confidence {int(float(item.get("confidence") or 0) * 100)}%</div>
        <p>{escape(str(item.get("hypothesis") or ""))}</p>
        <p class="meta">Entities: {escape(entities)}</p>
        <ul>{next_steps}</ul>
    </div>
    """


def _render_source_status(item: dict[str, Any]) -> str:
    status = escape(str(item.get("status") or "unknown"))
    return f"""
    <div class="stack-card">
        <strong>{escape(str(item.get("source") or "source"))}</strong>
        <div class="meta status-{status}">{status}</div>
        <div class="meta">{escape(str(item.get("detail") or ""))}</div>
    </div>
    """


def _render_forecast_row(item: dict[str, Any]) -> str:
    return f"""
    <div class="row">
        <div><strong>{escape(str(item.get("name") or "Model"))}</strong><div class="meta">{escape(str(item.get("note") or ""))}</div></div>
        <div>{escape(str(item.get("domain") or ""))}</div>
        <div>Accuracy {int(float(item.get("accuracy") or 0) * 100)}% · Calibration {int(float(item.get("calibration") or 0) * 100)}%</div>
    </div>
    """


def _render_government_action(item: dict[str, Any]) -> str:
    amount = float(item.get("amount") or 0.0)
    amount_text = (
        f"${amount / 1_000_000_000:.1f}B"
        if amount
        else escape(str(item.get("timing") or ""))
    )
    reaction = escape(
        str(item.get("market_reaction_window") or item.get("impact") or "")
    )
    return f"""
    <div class="row">
        <div><strong>{escape(str(item.get("title") or "Action"))}</strong><div class="meta">{escape(str(item.get("sector") or item.get("agency") or ""))}</div></div>
        <div>{amount_text}</div>
        <div>{reaction}</div>
    </div>
    """


def _render_journal_row(item: dict[str, Any]) -> str:
    return f"""
    <div class="row">
        <div><strong>{escape(str(item.get("title") or "Journal entry"))}</strong><div class="meta">{escape(str(item.get("timestamp") or ""))}</div></div>
        <div>{int(float(item.get("probability") or 0) * 100)}%</div>
        <div>{escape(str(item.get("horizon") or ""))}</div>
    </div>
    """
