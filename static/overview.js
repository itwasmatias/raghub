(function () {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value, fallback = "Unavailable") => {
    const raw = value === null || value === undefined || value === "" ? fallback : String(value);
    return raw.replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  };
  const status = (value) => '<span class="status ' + escapeHtml(value, "unavailable") + '">' + escapeHtml(value) + "</span>";
  const count = (item) => item && item.status === "available" ? String(item.value) : "Unavailable";
  const percent = (value) => value === null || value === undefined ? "Unavailable" : (Number(value) * 100).toFixed(1) + "%";
  const price = (value) => value === null || value === undefined ? "Unavailable" : (Number(value) > 0 ? "+" : "") + Number(value);
  const time = (value) => {
    if (!value) return "Unavailable";
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? escapeHtml(value) : parsed.toLocaleString();
  };

  function empty(message) {
    return '<div class="empty">' + escapeHtml(message) + "</div>";
  }

  function renderLeague(item) {
    const metrics = [
      ["Upcoming", item.counts.upcoming_events],
      ["Evaluations", item.counts.evaluations],
      ["Qualified", item.counts.qualified],
      ["No bet", item.counts.no_bet],
      ["Resolved", item.counts.resolved_predictions],
    ].map(([label, value]) => '<div class="metric"><span>' + label + '</span><strong>' + count(value) + "</strong></div>").join("");
    const predictions = (item.predictions || []).map((prediction) => {
      const decisions = (prediction.decisions || []).map((decision) =>
        '<div class="decision"><div class="decision-line"><strong>' + escapeHtml(decision.selection)
        + '</strong>' + status(decision.status) + '</div><div class="numbers"><span>Best '
        + escapeHtml(decision.best_sportsbook) + " " + price(decision.best_price)
        + '</span><span>Model ' + percent(decision.model_probability) + '</span><span>Market '
        + percent(decision.market_probability) + '</span><span>Edge ' + percent(decision.edge)
        + '</span><span>EV ' + percent(decision.expected_value) + '</span><span>Quality '
        + percent(decision.data_quality) + "</span></div></div>"
      ).join("") || empty("No persisted qualification result for this event.");
      return '<article class="prediction"><div class="prediction-head"><div><h3>'
        + escapeHtml(prediction.away_team, "Team unavailable") + " at "
        + escapeHtml(prediction.home_team, "Team unavailable") + '</h3><p>'
        + time(prediction.start_time) + " · " + escapeHtml(prediction.complete_books)
        + ' complete books</p></div>' + status(prediction.freshness)
        + '</div><div class="decision-list">' + decisions + '</div><a class="panel-link" href="'
        + escapeHtml(prediction.detail_url, "#") + '">Inspect event data</a></article>';
    }).join("") || empty("No persisted upcoming events.");
    const performance = item.performance;
    return '<article class="panel"><div class="panel-head"><div><p class="eyebrow">'
      + escapeHtml(item.league) + ' Intelligence</p><h2>' + escapeHtml(item.league)
      + '</h2></div>' + status(item.status) + '</div><p class="provider">Provider '
      + escapeHtml(item.provider.name) + " · " + escapeHtml(item.provider.freshness)
      + " · last success " + time(item.provider.last_successful_refresh) + "<br>Model "
      + escapeHtml(item.model.status) + " · " + escapeHtml(item.model.version)
      + '</p><div class="metric-grid">' + metrics + '</div><div class="stack">'
      + predictions + '</div><div class="kv"><span>Performance sample</span><strong>'
      + (performance.status === "available" ? performance.sample_size : "Unavailable")
      + '</strong></div><div class="kv"><span>Brier score</span><strong>'
      + (performance.brier_score === null ? "Unavailable" : Number(performance.brier_score).toFixed(3))
      + '</strong></div><a class="panel-link" href="' + escapeHtml(item.detail_url)
      + '">Open ' + escapeHtml(item.league) + " details</a></article>";
  }

  function renderSituation(item) {
    const forecasts = (item.active_forecasts || []).map((forecast) =>
      '<article class="forecast"><div class="decision-line"><strong>'
      + escapeHtml(forecast.question) + '</strong>' + status(forecast.calibration_status)
      + '</div><p>' + percent(forecast.current_probability) + " · resolves "
      + time(forecast.resolution_deadline) + '</p><p>Supporting '
      + Number(forecast.supporting_evidence_count || 0) + " · contradicting "
      + Number(forecast.contradicting_evidence_count || 0) + "</p></article>"
    ).join("") || empty("No persisted active forecasts.");
    byId("situationRoom").innerHTML = '<article class="panel"><div class="panel-head"><div><h3>Persisted Situation Room</h3><p class="muted">As of '
      + time(item.as_of) + '</p></div>' + status(item.status)
      + '</div><div class="situation-grid"><div class="forecast-list">' + forecasts
      + '</div><div><div class="kv"><span>Intelligence briefs</span><strong>'
      + escapeHtml(item.briefs_status) + '</strong></div><div class="kv"><span>Investigations</span><strong>'
      + escapeHtml(item.investigations_status) + '</strong></div><p class="muted">Briefs and investigations remain unavailable here until a persisted read model exists. Opening the Situation Room may run a verified scan.</p></div></div></article>';
  }

  function renderLower(payload) {
    const domains = payload.forecast_performance.domains.map((item) =>
      '<div class="kv"><span>' + escapeHtml(item.domain) + '</span><strong>'
      + (item.status === "available" ? item.sample_size + " resolved" : "Unavailable")
      + "</strong></div>"
    ).join("");
    byId("forecastPerformance").innerHTML = '<article class="panel"><div class="panel-head"><h3>Forecast Performance</h3>'
      + status(payload.forecast_performance.status) + '</div>' + domains
      + '<a class="panel-link" href="/calibration">Open calibration</a></article>';
    const evidence = payload.evidence;
    byId("evidenceStatus").innerHTML = '<article class="panel"><div class="panel-head"><h3>Evidence Status</h3>'
      + status(evidence.status) + '</div><div class="kv"><span>Persisted records</span><strong>'
      + count(evidence.persisted_records) + '</strong></div><div class="kv"><span>Supporting links</span><strong>'
      + count(evidence.supporting_links) + '</strong></div><div class="kv"><span>Contradicting links</span><strong>'
      + count(evidence.contradicting_links) + '</strong></div><a class="panel-link" href="/situation-room/evidence">Open evidence explorer</a></article>';
    const health = payload.system_health;
    const providers = (health.providers || []).map((provider) =>
      '<div class="kv"><span>' + escapeHtml(provider.name) + " · "
      + escapeHtml((provider.leagues || []).join(", ")) + '</span><strong>'
      + escapeHtml(provider.status) + " / " + escapeHtml(provider.freshness) + "</strong></div>"
    ).join("");
    byId("systemHealth").innerHTML = '<article class="panel"><div class="panel-head"><h3>System Health</h3>'
      + status(health.status) + '</div>' + providers
      + '<div class="kv"><span>Situation Room</span><strong>' + escapeHtml(health.situation_room.status)
      + '</strong></div><div class="kv"><span>Compute</span><strong>'
      + escapeHtml(health.compute.status) + "</strong></div></article>";
  }

  function render(payload) {
    byId("overviewStatus").className = "status " + escapeHtml(payload.status);
    byId("overviewStatus").textContent = payload.status;
    byId("overviewGeneratedAt").textContent = "Built " + time(payload.generated_at);
    byId("overviewNotices").innerHTML = (payload.notices || []).map((item) =>
      '<div class="notice ' + escapeHtml(item.status) + '"><strong>'
      + escapeHtml(item.scope) + " · " + escapeHtml(item.status)
      + "</strong><br>" + escapeHtml(item.message) + "</div>"
    ).join("");
    byId("leagueWNBA").innerHTML = renderLeague(payload.sports.WNBA);
    byId("leagueMLB").innerHTML = renderLeague(payload.sports.MLB);
    renderSituation(payload.situation_room);
    renderLower(payload);
  }

  function renderFailure(message) {
    byId("overviewStatus").className = "status error";
    byId("overviewStatus").textContent = "error";
    ["leagueWNBA", "leagueMLB", "situationRoom", "forecastPerformance", "evidenceStatus", "systemHealth"]
      .forEach((id) => { byId(id).innerHTML = '<article class="panel">' + empty(message) + "</article>"; });
  }

  fetch("/api/overview", {headers: {"Accept": "application/json"}})
    .then((response) => {
      if (!response.ok) throw new Error("Overview state could not be loaded.");
      return response.json();
    })
    .then(render)
    .catch((error) => renderFailure(error.message));
}());
