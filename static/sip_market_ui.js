(function () {
    const page = document.body.dataset.page || "";

    async function readJson(url, init) {
        const response = await fetch(url, init);
        if (!response.ok) {
            const text = await response.text();
            throw new Error(text || `Request failed: ${response.status}`);
        }
        return response.json();
    }

    function valueOrUnavailable(value) {
        if (value === null || value === undefined || value === "") {
            return "Unavailable";
        }
        return value;
    }

    function formatPct(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return "Unavailable";
        }
        return `${(Number(value) * 100).toFixed(1)}%`;
    }

    function formatMoney(value) {
        if (value === null || value === undefined || value === "") {
            return "Unavailable";
        }
        const numeric = Number(value);
        if (Number.isNaN(numeric)) {
            return String(value);
        }
        return `$${numeric.toFixed(2)}`;
    }

    function formatEdge(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return "Unavailable";
        }
        const signed = Number(value) * 100;
        const sign = signed >= 0 ? "+" : "";
        return `${sign}${signed.toFixed(2)} percentage points`;
    }

    function americanToDecimal(odds) {
        if (!odds || Number(odds) === 0) {
            return null;
        }
        const n = Number(odds);
        if (n > 0) {
            return 1 + n / 100;
        }
        return 1 + 100 / Math.abs(n);
    }

    function impliedFromAmerican(odds) {
        if (!odds || Number(odds) === 0) {
            return null;
        }
        const n = Number(odds);
        if (n > 0) {
            return 100 / (100 + n);
        }
        return Math.abs(n) / (Math.abs(n) + 100);
    }

    function chooseEdgeChip(edge) {
        if (edge === null || edge === undefined || Number.isNaN(Number(edge))) {
            return '<span class="status-chip warning">No Model</span>';
        }
        if (Number(edge) > 0) {
            return '<span class="status-chip good">Positive Edge</span>';
        }
        if (Number(edge) < 0) {
            return '<span class="status-chip bad">Negative Edge</span>';
        }
        return '<span class="status-chip warning">Flat Edge</span>';
    }

    const parlayState = [];

    function addParlayLeg(leg) {
        if (!leg.marketId || !leg.outcomeId) {
            return;
        }
        const existing = parlayState.find((entry) => entry.outcomeId === leg.outcomeId);
        if (existing) {
            return;
        }
        parlayState.push(leg);
        renderParlay();
    }

    function removeParlayLeg(outcomeId) {
        const index = parlayState.findIndex((entry) => entry.outcomeId === outcomeId);
        if (index >= 0) {
            parlayState.splice(index, 1);
            renderParlay();
        }
    }

    function renderParlay() {
        const legsRoot = document.getElementById("parlay-legs");
        if (!legsRoot) {
            return;
        }
        if (!parlayState.length) {
            legsRoot.innerHTML = "<p>No selections yet.</p>";
            const combined = document.getElementById("parlay-combined");
            const implied = document.getElementById("parlay-implied");
            const warning = document.getElementById("parlay-warning");
            if (combined) combined.textContent = "Combined odds: --";
            if (implied) implied.textContent = "Naive implied probability: --";
            if (warning) warning.textContent = "No selections";
            return;
        }

        legsRoot.innerHTML = parlayState
            .map(
                (leg) => `
        <div class="parlay-leg">
          <span>${leg.label} (${leg.odds > 0 ? "+" : ""}${leg.odds})</span>
          <button type="button" class="pill remove-leg" data-outcome-id="${leg.outcomeId}">Remove</button>
        </div>
      `
            )
            .join("");

        legsRoot.querySelectorAll(".remove-leg").forEach((button) => {
            button.addEventListener("click", () => removeParlayLeg(button.dataset.outcomeId));
        });

        let combinedDecimal = 1;
        for (const leg of parlayState) {
            const decimal = americanToDecimal(leg.odds);
            if (decimal) {
                combinedDecimal *= decimal;
            }
        }
        const naiveImplied = combinedDecimal > 0 ? 1 / combinedDecimal : null;

        const combined = document.getElementById("parlay-combined");
        const implied = document.getElementById("parlay-implied");
        const warning = document.getElementById("parlay-warning");
        if (combined) combined.textContent = `Combined odds: ${combinedDecimal.toFixed(4)} dec`;
        if (implied) implied.textContent = `Naive implied probability: ${formatPct(naiveImplied)}`;
        if (warning) {
            const uniqueMarketCount = new Set(parlayState.map((item) => item.marketId)).size;
            warning.textContent =
                uniqueMarketCount < parlayState.length
                    ? "Correlation warning: repeated market relationship detected"
                    : "Structure valid";
            warning.className = uniqueMarketCount < parlayState.length ? "status-chip bad" : "status-chip good";
        }
    }

    async function loadPortfolioPill() {
        const pill = document.getElementById("portfolio-pill");
        if (!pill) {
            return;
        }
        try {
            const portfolio = await readJson("/api/sip/portfolio");
            pill.textContent = `Cash ${formatMoney(portfolio.cash_balance)} | Reserved ${formatMoney(portfolio.reserved_balance)}`;
        } catch (_error) {
            pill.textContent = "Portfolio unavailable";
        }
    }

    function marketQueryFromControls() {
        const query = new URLSearchParams();
        const ids = [
            ["sport", "sport-filter"],
            ["league", "league-filter"],
            ["date", "date-filter"],
            ["team", "team-filter"],
            ["player", "player-filter"],
            ["market_type", "market-type-filter"],
            ["provider", "provider-filter"],
            ["status", "status-filter"],
            ["sort_by", "sort-filter"],
            ["search", "search-filter"],
        ];
        for (const [key, id] of ids) {
            const element = document.getElementById(id);
            if (!element) continue;
            const value = (element.value || "").trim();
            if (value) query.set(key, value);
        }
        return query;
    }

    function attachOutcomeActions(root) {
        root.querySelectorAll(".select-outcome").forEach((button) => {
            button.addEventListener("click", (event) => {
                const row = event.target.closest(".outcome-row");
                const select = document.getElementById("ticket-outcome");
                const oddsInput = document.getElementById("ticket-odds");
                if (!row || !select || !oddsInput) {
                    return;
                }
                const outcomeId = row.dataset.outcomeId;
                for (const option of select.options) {
                    if (option.value === outcomeId) {
                        select.value = option.value;
                        break;
                    }
                }
                if (row.dataset.odds) {
                    oddsInput.value = row.dataset.odds;
                }
                const warning = document.getElementById("ticket-warning");
                if (warning) {
                    warning.className = "status-chip good";
                    warning.textContent = `Selected ${row.dataset.outcomeName}`;
                }
                updateTicketMetrics();
            });
        });

        root.querySelectorAll(".add-parlay").forEach((button) => {
            button.addEventListener("click", (event) => {
                const row = event.target.closest(".outcome-row");
                if (!row) {
                    return;
                }
                addParlayLeg({
                    marketId: row.dataset.marketId,
                    outcomeId: row.dataset.outcomeId,
                    label: row.dataset.outcomeName,
                    odds: Number(row.dataset.odds || 0),
                });
            });
        });
    }

    function marketCard(market) {
        const outcomes = market.outcomes || [];
        const outcomeRows = outcomes
            .map(
                (outcome) => `
          <div class="outcome-row" data-market-id="${market.id}" data-outcome-id="${outcome.id}" data-outcome-name="${outcome.name}" data-odds="${outcome.best_odds ?? ""}">
            <strong>${outcome.name}</strong>
            <span><b>Market probability:</b> ${formatPct(outcome.sportsbook_consensus_probability ?? outcome.market_probability)}</span>
            <span><b>SIP probability:</b> ${formatPct(outcome.sip_probability)}</span>
            <span><b>Estimated edge:</b> ${formatEdge(outcome.edge)}</span>
            <span><b>EV:</b> ${valueOrUnavailable(outcome.expected_value)}</span>
            ${chooseEdgeChip(outcome.edge)}
            <button type="button" class="select-outcome">Select</button>
            <button type="button" class="add-parlay">Add Leg</button>
          </div>
        `
            )
            .join("");

        return `
      <article class="market-card" data-market-id="${market.id}">
        <div class="market-head">
          <strong>${market.title}</strong>
          <span class="status-chip ${market.qualified_label === "Qualified" ? "good" : "warning"}">${market.qualified_label}</span>
        </div>
        <p>${market.description}</p>
        <p><small>${market.league} | ${market.market_type} | status ${market.status} | starts ${market.opens_at}</small></p>
        <p><small>${market.has_open_position ? "Open position indicator: active" : "Open position indicator: none"}</small></p>
        <div class="market-outcomes">${outcomeRows}</div>
        <p><a class="pill" href="/sip/markets/${market.id}">Open Market Detail</a> <a class="pill" href="/sip/events/${market.event_id}">Open Event Detail</a></p>
      </article>
    `;
    }

    function refreshTicketOutcomeOptions(markets) {
        const select = document.getElementById("ticket-outcome");
        if (!select) {
            return;
        }
        const options = [];
        for (const market of markets) {
            for (const outcome of market.outcomes || []) {
                options.push(
                    `<option value="${outcome.id}" data-market-id="${market.id}" data-odds="${outcome.best_odds ?? ""}">${market.title} - ${outcome.name}</option>`
                );
            }
        }
        select.innerHTML = options.join("");
    }

    async function renderMarketFeed() {
        const root = document.getElementById("market-feed");
        if (!root) {
            return;
        }
        const payload = await readJson(`/api/sip/markets?${marketQueryFromControls().toString()}`);
        if (!payload.markets || !payload.markets.length) {
            root.innerHTML = `<article class="market-card"><h2>Unavailable</h2><p>${payload.unavailable_state || "No persisted market records are available yet."}</p></article>`;
            return;
        }
        root.innerHTML = payload.markets.map(marketCard).join("");
        attachOutcomeActions(root);
        refreshTicketOutcomeOptions(payload.markets);
        updateTicketMetrics();
    }

    async function renderProps() {
        const root = document.getElementById("props-root");
        if (!root) {
            return;
        }
        const payload = await readJson("/api/sip/props?sort_by=expected_value");
        if (!payload.markets || !payload.markets.length) {
            root.innerHTML = "<article class=\"market-card\">No prop markets available.</article>";
            return;
        }
        root.innerHTML = payload.markets
            .map(
                (market) => `
            <article class="market-card">
              <h3>${market.title}</h3>
              <p>${market.description}</p>
              <p><small>${market.league} | ${market.market_type} | ${market.status}</small></p>
              <p><a class="pill" href="/sip/markets/${market.id}">Open Market</a></p>
            </article>
          `
            )
            .join("");
    }

    async function renderDetail() {
        const marketId = document.body.dataset.marketId;
        const root = document.getElementById("market-detail-root");
        if (!marketId || !root) {
            return;
        }
        const payload = await readJson(`/api/sip/markets/${encodeURIComponent(marketId)}`);
        const market = payload.market;
        const outcomes = payload.outcomes || [];
        const snapshots = payload.probability_snapshots || [];
        const latestByOutcome = {};
        for (const item of snapshots) {
            if (!latestByOutcome[item.outcome_id]) {
                latestByOutcome[item.outcome_id] = item;
            }
        }

        root.innerHTML = `
      <h1>${market.title}</h1>
      <p>${market.description}</p>
      <p><small>${market.league} | ${market.market_type} | status ${market.status} | closes ${market.closes_at}</small></p>
      <h2>Outcome Comparison</h2>
      <div class="market-outcomes">
        ${outcomes
                .map((outcome) => {
                    const snapshot = latestByOutcome[outcome.id];
                    return `
              <div class="outcome-row">
                <strong>${outcome.name}</strong>
                <span>Market probability: ${formatPct(snapshot?.sportsbook_consensus_probability)}</span>
                <span>SIP probability: ${formatPct(snapshot?.sip_adjusted_probability)}</span>
                <span>Estimated edge: ${formatEdge(snapshot?.edge)}</span>
                <span>EV per $1: ${valueOrUnavailable(snapshot?.expected_value)}</span>
              </div>
            `;
                })
                .join("")}
      </div>
      <h2>Settlement Rule</h2>
      <p>${market.settlement_rule}</p>
      <h2>Resolution Source</h2>
      <p>${market.resolution_source}</p>
      <h2>Supporting Factors</h2>
      <ul>${(payload.supporting_factors || []).map((item) => `<li>${item}</li>`).join("")}</ul>
      <h2>Contradicting Factors</h2>
      <ul>${(payload.contradicting_factors || []).map((item) => `<li>${item}</li>`).join("")}</ul>
      <h2>Sportsbook Comparison</h2>
      <p>${payload.quotes?.length || 0} quote observations</p>
      <h2>Recent Activity</h2>
      <p>${payload.recent_activity?.length || 0} events</p>
    `;

        const ticketOutcome = document.getElementById("ticket-outcome");
        if (ticketOutcome) {
            ticketOutcome.innerHTML = outcomes
                .map((outcome) => {
                    const snapshot = latestByOutcome[outcome.id];
                    const odds = snapshot ? snapshot.american_odds : -110;
                    return `<option value="${outcome.id}" data-market-id="${market.id}" data-odds="${odds}">${outcome.name}</option>`;
                })
                .join("");
        }
        updateTicketMetrics();
    }

    async function renderEventDetail() {
        const root = document.getElementById("event-detail-root");
        const eventId = document.body.dataset.eventId;
        if (!root || !eventId) {
            return;
        }
        const payload = await readJson(`/api/sip/events/${encodeURIComponent(eventId)}`);
        const event = payload.event;
        root.innerHTML = `
      <h1>${event.away_team} at ${event.home_team}</h1>
      <p><small>${event.league} | ${event.sport} | starts ${event.starts_at} | status ${event.status}</small></p>
      <h2>Event Markets</h2>
      <div class="market-feed">
        ${(payload.markets || [])
                .map(
                    (market) => `<article class="market-card"><h3>${market.title}</h3><p>${market.market_type} | ${market.status}</p><a class="pill" href="/sip/markets/${market.id}">Open</a></article>`
                )
                .join("")}
      </div>
    `;
    }

    function renderPositionsList(root, positions) {
        if (!positions.length) {
            root.innerHTML = "<article class=\"activity-item\">No positions available.</article>";
            return;
        }
        root.innerHTML = positions
            .map(
                (position) => `
          <article class="activity-item">
            <header><strong>${position.id}</strong> <small>${position.status}</small></header>
            <p>Entry odds ${valueOrUnavailable(position.entry_odds ?? position.average_accepted_odds)} | Current odds ${valueOrUnavailable(position.current_odds)}</p>
            <p>Edge at entry ${valueOrUnavailable(position.edge_at_entry)} | Current edge ${valueOrUnavailable(position.current_edge)}</p>
            <p>Potential return ${formatMoney(position.potential_return)} | Stake ${formatMoney(position.stake)}</p>
            <p>Mode ${position.mode} | Market ${position.market_id}</p>
          </article>
        `
            )
            .join("");
    }

    async function renderOpenPositions() {
        const root = document.getElementById("open-positions-root");
        if (!root) {
            return;
        }
        const payload = await readJson("/api/sip/open-positions");
        renderPositionsList(root, payload.positions || []);
    }

    async function renderSettledPositions() {
        const root = document.getElementById("settled-positions-root");
        if (!root) {
            return;
        }
        const payload = await readJson("/api/sip/settled-positions");
        renderPositionsList(root, payload.positions || []);
    }

    async function renderPortfolio() {
        const root = document.getElementById("portfolio-root");
        if (!root) {
            return;
        }
        const payload = await readJson("/api/sip/portfolio");
        root.innerHTML = `
      <h1>Portfolio</h1>
      <div class="portfolio-grid">
        <article class="metric-box"><h3>Opening bankroll</h3><p>${formatMoney(payload.opening_bankroll)}</p></article>
        <article class="metric-box"><h3>Deposits</h3><p>${formatMoney(payload.deposits)}</p></article>
        <article class="metric-box"><h3>Withdrawals</h3><p>${formatMoney(payload.withdrawals)}</p></article>
        <article class="metric-box"><h3>Available balance</h3><p>${formatMoney(payload.cash_balance)}</p></article>
        <article class="metric-box"><h3>Reserved balance</h3><p>${formatMoney(payload.reserved_balance)}</p></article>
        <article class="metric-box"><h3>Open stake</h3><p>${formatMoney(payload.open_stake)}</p></article>
        <article class="metric-box"><h3>Max possible loss</h3><p>${formatMoney(payload.maximum_possible_loss)}</p></article>
        <article class="metric-box"><h3>Max possible profit</h3><p>${formatMoney(payload.maximum_possible_profit)}</p></article>
        <article class="metric-box"><h3>Realized P/L</h3><p>${formatMoney(payload.realized_profit_loss)}</p></article>
        <article class="metric-box"><h3>Synthetic market change</h3><p>${formatMoney(payload.unrealized_synthetic_market_change)}</p></article>
        <article class="metric-box"><h3>SIP-adjusted change</h3><p>${formatMoney(payload.unrealized_sip_adjusted_change)}</p></article>
        <article class="metric-box"><h3>Voids / Pushes</h3><p>${payload.voided_wagers} / ${payload.pushed_wagers}</p></article>
      </div>
      <h2>Exposure Summary</h2>
      <pre>${JSON.stringify(payload.exposure, null, 2)}</pre>
      <h2>Synthetic valuation records</h2>
      <pre>${JSON.stringify(payload.synthetic_valuations || [], null, 2)}</pre>
    `;
    }

    async function renderExposure() {
        const root = document.getElementById("exposure-root");
        if (!root) {
            return;
        }
        const payload = await readJson("/api/sip/exposure");
        root.innerHTML = `
      <h1>Exposure Dashboard</h1>
      <p>Open positions: ${payload.open_positions} | Open stake: ${formatMoney(payload.open_stake)}</p>
      <pre>${JSON.stringify(payload.exposure, null, 2)}</pre>
    `;
    }

    async function renderActivity() {
        const root = document.getElementById("activity-root");
        if (!root) {
            return;
        }
        const payload = await readJson("/api/sip/activity");
        const combined = [...(payload.events || []), ...(payload.domain_events || []).map((event) => ({
            created_at: event.recorded_at,
            kind: `domain:${event.event_type}`,
            payload: event,
        }))];
        combined.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
        if (!combined.length) {
            root.innerHTML = "<article class=\"activity-item\">No activity records yet.</article>";
            return;
        }
        root.innerHTML = combined
            .map(
                (event) => `
          <article class="activity-item">
            <header><strong>${event.kind}</strong> <small>${event.created_at}</small></header>
            <pre>${JSON.stringify(event.payload, null, 2)}</pre>
          </article>
        `
            )
            .join("");
    }

    async function renderModelHistory() {
        const root = document.getElementById("model-history-root");
        if (!root) {
            return;
        }
        const markets = await readJson("/api/sip/markets?sort_by=sip_edge");
        const firstMarket = (markets.markets || [])[0];
        if (!firstMarket) {
            root.innerHTML = "<article class=\"activity-item\">No markets available for model history.</article>";
            return;
        }
        const history = await readJson(`/api/sip/markets/${encodeURIComponent(firstMarket.id)}/probability-history`);
        root.innerHTML = `
      <article class="activity-item">
        <h2>${firstMarket.title}</h2>
        <p>Market probability and SIP probability timelines are stored separately.</p>
        <pre>${JSON.stringify(history.history || [], null, 2)}</pre>
      </article>
    `;
    }

    function updateTicketMetrics() {
        const stakeInput = document.getElementById("ticket-stake");
        const oddsInput = document.getElementById("ticket-odds");
        const modeSelect = document.getElementById("ticket-mode");
        const stake = Number(stakeInput?.value || 0);
        const odds = Number(oddsInput?.value || 0);

        let profit = null;
        if (odds > 0) {
            profit = stake * (odds / 100);
        } else if (odds < 0) {
            profit = stake * (100 / Math.abs(odds));
        }
        const ret = profit === null ? null : stake + profit;
        const implied = impliedFromAmerican(odds);

        const profitNode = document.getElementById("metric-profit");
        const returnNode = document.getElementById("metric-return");
        const breakEvenNode = document.getElementById("metric-breakeven");
        const bankrollNode = document.getElementById("metric-bankroll");
        const reservedNode = document.getElementById("metric-reserved");
        if (profitNode) profitNode.textContent = formatMoney(profit);
        if (returnNode) returnNode.textContent = formatMoney(ret);
        if (breakEvenNode) breakEvenNode.textContent = formatPct(implied);

        readJson("/api/sip/portfolio")
            .then((portfolio) => {
                if (bankrollNode) bankrollNode.textContent = formatMoney(portfolio.cash_balance);
                const cash = Number(portfolio.cash_balance || 0);
                const reserved = Number(portfolio.reserved_balance || 0);
                if (reservedNode) reservedNode.textContent = formatMoney(reserved + (Number.isFinite(stake) ? stake : 0));

                const warning = document.getElementById("ticket-warning");
                if (!warning) {
                    return;
                }
                if (modeSelect && modeSelect.value === "recorded_real") {
                    warning.textContent = "Manual recorded-real mode. This does not place a sportsbook order.";
                    warning.className = "status-chip warning";
                } else {
                    warning.textContent = "Practice mode. Execution remains disabled.";
                    warning.className = "status-chip good";
                }
                if (cash < stake) {
                    warning.textContent = "Validation warning: stake exceeds available balance.";
                    warning.className = "status-chip bad";
                }
            })
            .catch(() => {
                if (bankrollNode) bankrollNode.textContent = "Unavailable";
            });
    }

    function bindTicketActions() {
        const mode = document.getElementById("ticket-mode");
        const submit = document.getElementById("ticket-submit");

        if (mode && submit) {
            mode.addEventListener("change", () => {
                submit.textContent = mode.value === "practice" ? "Record Practice Position" : "Record Manual Position";
                updateTicketMetrics();
            });
        }

        const stake = document.getElementById("ticket-stake");
        const odds = document.getElementById("ticket-odds");
        if (stake) stake.addEventListener("input", updateTicketMetrics);
        if (odds) odds.addEventListener("input", updateTicketMetrics);

        if (!submit) {
            return;
        }
        submit.addEventListener("click", async () => {
            const outcomeSelect = document.getElementById("ticket-outcome");
            if (!outcomeSelect || !mode || !stake || !odds) {
                return;
            }
            const selected = outcomeSelect.options[outcomeSelect.selectedIndex];
            const marketId = selected?.dataset?.marketId || document.body.dataset.marketId;
            const payload = {
                market_id: marketId,
                outcome_id: outcomeSelect.value,
                mode: mode.value,
                stake: stake.value,
                requested_odds: Number(odds.value),
                accepted_odds: Number(odds.value),
            };

            const warning = document.getElementById("ticket-warning");
            try {
                const result = await readJson("/api/sip/order-intents", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                if (warning) {
                    warning.className = "status-chip good";
                    warning.textContent = `${result.actions.primary} complete. Execution remains disabled.`;
                }
                await loadPortfolioPill();
                updateTicketMetrics();
            } catch (error) {
                if (warning) {
                    warning.className = "status-chip bad";
                    warning.textContent = `Validation failed: ${String(error.message).slice(0, 180)}`;
                }
            }
        });
    }

    async function boot() {
        await loadPortfolioPill();

        if (
            page === "markets" ||
            page === "market-detail" ||
            page === "props" ||
            page === "parlay-builder"
        ) {
            bindTicketActions();
        }

        if (page === "markets") {
            await renderMarketFeed();
            [
                "sport-filter",
                "league-filter",
                "date-filter",
                "team-filter",
                "player-filter",
                "market-type-filter",
                "provider-filter",
                "status-filter",
                "sort-filter",
            ].forEach((id) => {
                document.getElementById(id)?.addEventListener("change", renderMarketFeed);
            });
            document.getElementById("search-filter")?.addEventListener("input", renderMarketFeed);
            renderParlay();
        }
        if (page === "market-detail") {
            await renderDetail();
        }
        if (page === "event-detail") {
            await renderEventDetail();
        }
        if (page === "props") {
            await renderProps();
        }
        if (page === "parlay-builder") {
            renderParlay();
        }
        if (page === "portfolio") {
            await renderPortfolio();
        }
        if (page === "open-positions") {
            await renderOpenPositions();
        }
        if (page === "settled-positions") {
            await renderSettledPositions();
        }
        if (page === "exposure") {
            await renderExposure();
        }
        if (page === "activity") {
            await renderActivity();
        }
        if (page === "model-history") {
            await renderModelHistory();
        }
    }

    boot().catch((error) => {
        const host =
            document.getElementById("market-feed") ||
            document.getElementById("market-detail-root") ||
            document.getElementById("event-detail-root") ||
            document.getElementById("props-root") ||
            document.getElementById("portfolio-root") ||
            document.getElementById("open-positions-root") ||
            document.getElementById("settled-positions-root") ||
            document.getElementById("exposure-root") ||
            document.getElementById("activity-root") ||
            document.getElementById("model-history-root");
        if (host) {
            host.innerHTML = `<article class="market-card"><h2>Unavailable</h2><p>${String(error.message || error)}</p></article>`;
        }
    });
})();
