(function () {
    "use strict";

    const byId = (id) => document.getElementById(id);
    const root = byId("appRoot");
    const state = {snapshot: null, timer: null};

    function escapeHtml(value, fallback = "Not available") {
        const raw = value === null || value === undefined || value === ""
            ? fallback : String(value);
        return raw.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;").replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function percent(value) {
        return Math.round(Number(value || 0) * 100) + "%";
    }

    function emptyCard(title, why, nextAction) {
        return '<article class="card empty-state"><h3>' + escapeHtml(title)
            + '</h3><p>' + escapeHtml(why)
            + '</p>' + (nextAction ? '<p><strong>Next action:</strong> '
            + escapeHtml(nextAction) + '</p>' : "") + '</article>';
    }

    function evidenceLinks(items) {
        if (!Array.isArray(items) || !items.length) {
            return '<p class="evidence-warning"><strong>Evidence unavailable.</strong> This item cannot be treated as verified.</p>';
        }
        return '<ul class="evidence-links">' + items.map((item) => {
            const id = item.evidence_id || "";
            const href = id ? "/api/evidence/" + encodeURIComponent(id) : item.url;
            return '<li><a href="' + escapeHtml(href, "#")
                + '" target="_blank" rel="noopener noreferrer">'
                + escapeHtml(item.title || item.source || "Evidence record")
                + '</a> <span class="muted">' + escapeHtml(item.source, "source")
                + ' · ' + escapeHtml(item.observed_at || item.explanation, "timestamp unavailable")
                + '</span></li>';
        }).join("") + "</ul>";
    }

    function renderBrief(snapshot) {
        const container = byId("intelligenceBrief");
        const items = snapshot.intelligence_brief || [];
        if (!items.length) {
            container.innerHTML = emptyCard(
                "No verified developments",
                "No item passed the persisted-evidence integrity and relevance gates.",
                "Check connector health, then run another verified scan."
            );
            return;
        }
        container.innerHTML = items.map((item) => {
            const evidence = (item.evidence_ids || []).map((id) => ({
                evidence_id: id,
                title: "Inspect evidence " + id,
                source: item.primary_source,
                observed_at: item.last_meaningful_update,
            }));
            const change = item.related_forecast_change;
            return '<article class="brief-development"><div class="card-heading">'
                + '<span class="badge">' + percent(item.confidence) + ' confidence</span>'
                + '<h3>' + escapeHtml(item.headline) + '</h3></div>'
                + '<p><strong>What changed:</strong> ' + escapeHtml(item.summary) + '</p>'
                + '<p><strong>Why it matters:</strong> ' + escapeHtml(item.why_it_matters) + '</p>'
                + '<p><strong>Affected:</strong> ' + escapeHtml((item.affected_entities || []).join(", "), "No entity identified") + '</p>'
                + '<div class="brief-metadata"><span>' + Number(item.evidence_count || 0)
                + ' evidence record(s)</span><span>Primary: ' + escapeHtml(item.primary_source)
                + '</span><span>' + Number(item.source_diversity || 0) + ' independent source(s)</span>'
                + '<span>First seen: ' + escapeHtml(item.first_seen) + '</span>'
                + '<span>Last update: ' + escapeHtml(item.last_meaningful_update) + '</span></div>'
                + evidenceLinks(evidence)
                + '<p><strong>Related forecast change:</strong> '
                + (change === null || change === undefined ? "No linked stored forecast"
                    : ((Number(change) >= 0 ? "+" : "") + percent(change)))
                + '</p><p><strong>Monitor next:</strong> '
                + escapeHtml((item.what_to_monitor_next || []).join(" · "), "Await the next official update")
                + '</p></article>';
        }).join("");

        const summary = snapshot.plain_language_summary || {};
        byId("plainLanguageSummary").innerHTML = '<article class="summary-lead"><h3>'
            + escapeHtml(summary.headline, "Plain-language summary")
            + '</h3><p>' + escapeHtml(summary.current_context, "Verified developments are shown above.")
            + '</p></article>';
        const prompts = snapshot.suggestion_prompts || [];
        byId("suggestionPrompts").innerHTML = prompts.map((item) =>
            '<button type="button" class="suggestion-button" data-prompt="'
            + escapeHtml(item.prompt) + '"><strong>' + escapeHtml(item.label)
            + '</strong><span>' + escapeHtml(item.prompt) + '</span></button>'
        ).join("");
    }

    function renderForecasts(snapshot) {
        const forecasts = snapshot.active_forecasts || [];
        byId("forecastCount").textContent = forecasts.length
            ? forecasts.length + " openable record(s)" : "";
        if (!forecasts.length) {
            byId("forecastList").innerHTML = emptyCard(
                "No active forecasts",
                "No official-data forecast has been persisted with a deadline, resolution rule, and evidence.",
                "Configure FRED and run a verified scan."
            );
            return;
        }
        byId("forecastList").innerHTML = forecasts.map((item) => {
            const change = Number(item.probability_change || 0);
            const supporting = item.supporting_evidence || [];
            const contradicting = item.contradicting_evidence || [];
            return '<article class="forecast-card"><div class="forecast-probability">'
                + '<strong>' + percent(item.current_probability) + '</strong><span>current probability</span></div>'
                + '<div><div class="card-heading"><span class="badge warn">'
                + escapeHtml(item.calibration_status) + '</span><h3>'
                + escapeHtml(item.question) + '</h3></div>'
                + '<div class="forecast-comparison"><span>Previous ' + percent(item.previous_probability)
                + '</span><span>Change ' + (change >= 0 ? "+" : "") + percent(change)
                + '</span><span>Horizon ' + escapeHtml(item.forecast_horizon)
                + '</span></div>'
                + '<p><strong>Resolution deadline:</strong> ' + escapeHtml(item.resolution_deadline)
                + '<br><strong>Resolution source:</strong> <a href="'
                + escapeHtml((item.resolution_source || {}).url, "#")
                + '" target="_blank" rel="noopener noreferrer">'
                + escapeHtml((item.resolution_source || {}).name) + '</a>'
                + '<br><strong>Resolution criteria:</strong> ' + escapeHtml(item.resolution_criteria)
                + '</p><p class="calibration-note"><strong>Experimental forecast.</strong> '
                + escapeHtml(item.calibration_note) + '</p>'
                + '<p><strong>Why probability changed:</strong> ' + escapeHtml(item.why_probability_changed)
                + '</p><div class="evidence-columns"><div><h4>Supporting evidence</h4>'
                + evidenceLinks(supporting) + '</div><div><h4>Contradicting evidence</h4>'
                + (contradicting.length ? evidenceLinks(contradicting)
                    : '<p class="muted">No persisted contradicting evidence identified yet.</p>')
                + '</div></div><div class="trigger-grid"><div><strong>Would increase</strong><p>'
                + escapeHtml((item.increase_triggers || []).join(" · "))
                + '</p></div><div><strong>Would decrease</strong><p>'
                + escapeHtml((item.decrease_triggers || []).join(" · "))
                + '</p></div></div><p class="muted">Last update: '
                + escapeHtml(item.last_updated_at) + ' · Rule ' + escapeHtml(item.model_version)
                + '</p><a class="ghost-button" href="/api/forecasts/'
                + encodeURIComponent(item.forecast_id)
                + '/history" target="_blank">Open full probability history</a></div></article>';
        }).join("");
    }

    function renderWatchlist(snapshot) {
        const items = snapshot.watchlist_changes || [];
        byId("watchlistChangesList").innerHTML = items.length ? items.map((item) =>
            '<article class="card"><h3>' + escapeHtml(item.entity) + '</h3><p>'
            + escapeHtml(item.development) + '</p><p><strong>Importance:</strong> '
            + escapeHtml(item.importance) + '<br><strong>Forecast implication:</strong> '
            + escapeHtml(item.forecast_implication) + '<br><strong>Why watched:</strong> '
            + escapeHtml(item.watchlist_reason) + '<br><strong>Last checked:</strong> '
            + escapeHtml(item.last_checked) + '</p>' + evidenceLinks(item.evidence)
            + '</article>'
        ).join("") : emptyCard(
            "No material watchlist changes",
            "Verified developments did not match a configured watchlist entity.",
            "Continue monitoring; repeated reporting is not treated as a change."
        );
    }

    function renderInvestigations(snapshot) {
        const items = snapshot.investigations || [];
        byId("investigationList").innerHTML = items.length ? items.map((item) =>
            '<article class="card"><div class="card-heading"><span class="badge">'
            + escapeHtml(item.status) + '</span><h3>' + escapeHtml(item.title)
            + '</h3></div><p><strong>Objective:</strong> ' + escapeHtml(item.objective)
            + '<br><strong>Current hypothesis:</strong> ' + escapeHtml(item.hypothesis)
            + '<br><strong>Current conclusion:</strong> ' + escapeHtml(item.current_conclusion)
            + '<br><strong>Confidence:</strong> ' + percent(item.confidence)
            + '</p><p><strong>Open questions:</strong> '
            + escapeHtml((item.open_questions || []).join(" · "))
            + '<br><strong>Next planned action:</strong> '
            + escapeHtml((item.next_steps || []).join(" · "))
            + '<br><strong>Related forecasts:</strong> '
            + escapeHtml((item.related_forecasts || []).join(", "), "None")
            + '</p>' + evidenceLinks(item.evidence_collected) + '</article>'
        ).join("") : emptyCard(
            "No active investigations",
            "No verified situation currently requires a research investigation."
        );
    }

    function renderUpcoming(snapshot) {
        const items = snapshot.upcoming_events || [];
        byId("upcomingEventsList").innerHTML = items.length ? items.map((item) =>
            '<article class="card"><h3>' + escapeHtml(item.title)
            + '</h3><p><strong>Type:</strong> ' + escapeHtml(item.event_type)
            + '<br><strong>Scheduled:</strong> ' + escapeHtml(item.scheduled_at)
            + '<br><strong>Source:</strong> <a href="'
            + escapeHtml((item.source || {}).url, "#") + '">'
            + escapeHtml((item.source || {}).name) + '</a></p></article>'
        ).join("") : emptyCard(
            "No scheduled catalysts",
            "No open forecast currently has a persisted resolution event."
        );
    }

    function renderAutonomy(snapshot) {
        const cycle = snapshot.latest_autonomy_cycle;
        const panel = byId("autonomyStatusPanel");
        if (!cycle || cycle.status === "empty") {
            panel.innerHTML = emptyCard(
                "No completed autonomous cycle",
                "The system has not yet persisted a research trace.",
                "Set an objective and run a cycle."
            );
            byId("autonomyActionsPanel").innerHTML = "";
            return;
        }
        panel.innerHTML = '<article class="card"><h3>' + escapeHtml(cycle.objective)
            + '</h3><p><strong>Why selected:</strong> ' + escapeHtml(cycle.objective_reason)
            + '<br><strong>Status:</strong> ' + escapeHtml(cycle.cycle_status)
            + '<br><strong>Started:</strong> ' + escapeHtml(cycle.started_at)
            + '<br><strong>Completed:</strong> ' + escapeHtml(cycle.completed_at)
            + '<br><strong>Evidence found:</strong> ' + Number(cycle.evidence_found || 0)
            + '<br><strong>Forecasts considered:</strong> '
            + escapeHtml((cycle.forecasts_considered || []).join(", "), "None")
            + '<br><strong>Forecasts updated:</strong> '
            + escapeHtml((cycle.forecasts_updated || []).join(", "), "None")
            + '<br><strong>Next action:</strong> ' + escapeHtml(cycle.next_action)
            + '</p></article>';
        byId("autonomyActionsPanel").innerHTML = (cycle.findings || []).map((item) =>
            '<article class="card"><h4>' + escapeHtml(item.title)
            + '</h4><p>' + escapeHtml(item.summary)
            + ' · ' + Number(item.evidence_count || 0) + ' evidence record(s)</p></article>'
        ).join("") || emptyCard("No findings", "The latest cycle completed without a verified finding.");
    }

    function renderHealth(snapshot) {
        const health = snapshot.system_health || {};
        const entries = health.connector_health || snapshot.source_status || [];
        byId("systemHealthPanel").innerHTML = [
            ["Last successful refresh", health.last_successful_refresh],
            ["Stale sources", (health.stale_sources || []).join(", ") || "None"],
            ["Evidence ingestion", health.evidence_ingestion_status],
            ["Forecast engine", health.forecast_engine_status],
            ["Scheduler", health.scheduler_status],
            ["Recent failures", (health.recent_failures || []).length],
        ].map(([label, value]) => '<div><span>' + escapeHtml(label)
            + '</span><strong>' + escapeHtml(value) + '</strong></div>').join("");
        byId("sourceStatusList").innerHTML = entries.map((item) =>
            '<article class="card"><h4>' + escapeHtml(item.source)
            + ' · ' + escapeHtml(item.status) + '</h4><p>'
            + escapeHtml(item.detail) + '<br><span class="muted">'
            + escapeHtml(item.checked_at) + '</span></p></article>'
        ).join("") || emptyCard("No connector telemetry", "No connector status records were returned.");
    }

    function render(snapshot) {
        state.snapshot = snapshot;
        renderBrief(snapshot);
        renderForecasts(snapshot);
        renderWatchlist(snapshot);
        renderInvestigations(snapshot);
        renderUpcoming(snapshot);
        renderAutonomy(snapshot);
        renderHealth(snapshot);
    }

    async function refresh() {
        byId("statusLine").textContent = "Retrieving official evidence and stored forecasts…";
        const params = new URLSearchParams({
            focus: byId("focusSelect").value,
            q: byId("queryInput").value.trim(),
        });
        const response = await fetch("/api/situation-room?" + params.toString(), {
            headers: {"Accept": "application/json"},
        });
        if (!response.ok) throw new Error("Situation Room request failed");
        render(await response.json());
        byId("statusLine").textContent = "Verified scan completed at " + new Date().toLocaleTimeString();
    }

    async function autonomyAction(action) {
        const response = await fetch("/api/situation-room/autonomy", {
            method: "POST",
            headers: {"Content-Type": "application/json", "Accept": "application/json"},
            body: JSON.stringify({
                action,
                objective: byId("autonomyObjectiveInput").value,
                interval_seconds: Number(byId("autonomyIntervalSelect").value),
                focus: byId("focusSelect").value,
                query: byId("queryInput").value.trim(),
            }),
        });
        if (!response.ok) throw new Error("Autonomy request failed");
        await refresh();
    }

    function renderScenario(prediction) {
        byId("predictionSummaryPanel").innerHTML = '<article class="card"><h3>Simulated scenario</h3><p>'
            + escapeHtml(prediction.summary) + '</p><p class="calibration-note">This is not a stored or calibrated forecast.</p></article>';
        const points = prediction.forecast_path || [];
        byId("forecastProbabilityChart").innerHTML = points.length
            ? '<div class="chart-bars">' + points.map((point) =>
                '<div style="height:' + Math.round(Number(point.base_case || 0) * 100)
                + '%" title="Day ' + Number(point.day) + ': '
                + percent(point.base_case) + '"></div>').join("") + '</div>'
            : emptyCard("No forecast path", "The scenario endpoint returned no checkpoints.");
        byId("predictionPathPanel").textContent = points.length + " simulated checkpoint(s)";
        byId("predictionDriversPanel").textContent = (prediction.key_drivers || []).join(" · ");
        byId("predictionActionsPanel").textContent = (prediction.suggested_actions || []).join(" · ");
    }

    byId("controlForm").addEventListener("submit", (event) => {
        event.preventDefault();
        refresh().catch((error) => { byId("statusLine").textContent = error.message; });
    });
    byId("refreshSelect").addEventListener("change", () => {
        if (state.timer) clearInterval(state.timer);
        state.timer = null;
        if (byId("refreshSelect").value !== "off") {
            state.timer = setInterval(refresh, Number(byId("refreshSelect").value) * 1000);
        }
    });
    byId("autonomyStartButton").addEventListener("click", () => autonomyAction("start"));
    byId("autonomyRunButton").addEventListener("click", () => autonomyAction("run"));
    byId("autonomyStopButton").addEventListener("click", () => autonomyAction("stop"));
    byId("suggestionPrompts").addEventListener("click", (event) => {
        const button = event.target.closest("[data-prompt]");
        if (!button) return;
        byId("queryInput").value = button.dataset.prompt;
        refresh();
    });
    byId("predictionForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const response = await fetch("/api/situation-room/predict", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                focus: byId("focusSelect").value,
                scenario: byId("predictionScenarioInput").value,
                horizon_days: Number(byId("predictionHorizonSelect").value),
                confidence_mode: byId("predictionModeSelect").value,
            }),
        });
        renderScenario(await response.json());
    });

    byId("focusSelect").value = root.dataset.focus || "global";
    byId("queryInput").value = root.dataset.query || "";
    refresh().catch((error) => { byId("statusLine").textContent = error.message; });
}());
