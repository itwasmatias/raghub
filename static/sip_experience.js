(function () {
    "use strict";

    function toPercent(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return "Unavailable";
        }
        return (Number(value) * 100).toFixed(1) + "%";
    }

    function clampPercent(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
            return 0;
        }
        const pct = Number(value) * 100;
        return Math.max(0, Math.min(100, pct));
    }

    function addCardGuidance() {
        const choices = document.querySelectorAll("article.choice");
        choices.forEach((choice) => {
            if (choice.querySelector(".sipx-analytics")) {
                return;
            }
            const statusBadge = choice.querySelector(".badge");
            const isQualified = statusBadge && /qualified/i.test(statusBadge.textContent || "");
            const bestText = (choice.querySelector(".number strong") || {}).textContent || "Unavailable";
            const modelText = (choice.querySelectorAll(".number strong")[1] || {}).textContent || "Unavailable";
            const marketText = (choice.querySelectorAll(".number strong")[2] || {}).textContent || "Unavailable";
            const edgeText = (choice.querySelectorAll(".number strong")[3] || {}).textContent || "Unavailable";

            const immediate = document.createElement("section");
            immediate.className = "sipx-immediate";
            immediate.innerHTML =
                "<h3>Immediate view</h3>" +
                "<p class='sipx-line'>Best available sportsbook price: <span class='sipx-emphasis'>" + bestText + "</span></p>" +
                "<p class='sipx-line'>Model probability: <span class='sipx-emphasis'>" + modelText + "</span> · Market-implied probability: <span class='sipx-emphasis'>" + marketText + "</span></p>" +
                "<p class='sipx-line'>Edge and expected value: <span class='sipx-emphasis'>" + edgeText + "</span></p>" +
                "<div class='sipx-status-row'>" +
                "<span class='sipx-status " + (isQualified ? "qualified" : "no-bet") + "'>" + (isQualified ? "qualified" : "no_bet") + "</span>" +
                "<span class='sipx-status info'>available</span>" +
                "</div>";

            const analytics = document.createElement("section");
            analytics.className = "sipx-analytics";
            analytics.innerHTML =
                "<h3>Analytical view</h3>" +
                "<p class='sipx-line'>SIP estimates this team wins based on model probability and sportsbook consensus.</p>" +
                "<div class='sipx-metric-bars'>" +
                "<div class='sipx-bar-wrap'><div class='sipx-bar-label'><span>Model probability</span><span data-model-pct>Unavailable</span></div><div class='sipx-bar'><div class='sipx-fill model' data-model-fill></div></div></div>" +
                "<div class='sipx-bar-wrap'><div class='sipx-bar-label'><span>Market-implied probability</span><span data-market-pct>Unavailable</span></div><div class='sipx-bar'><div class='sipx-fill market' data-market-fill></div></div></div>" +
                "<div class='sipx-bar-wrap'><div class='sipx-bar-label'><span>Data quality</span><span data-quality-pct>Unavailable</span></div><div class='sipx-bar'><div class='sipx-fill quality' data-quality-fill></div></div></div>" +
                "</div>";

            const details = document.createElement("details");
            details.className = "sipx-details";
            details.innerHTML =
                "<summary>Expanded details</summary>" +
                "<p class='sipx-line'>Important missing information: if lineup, pitcher, or injury inputs are missing, SIP preserves no-bet safeguards.</p>" +
                "<p class='sipx-line'>Data freshness and evidence timestamps are displayed in the quote table and diagnostics.</p>" +
                "<p class='sipx-line'>Stale quote warning: stale or unavailable quote windows reduce confidence and can force no-bet.</p>";

            choice.appendChild(immediate);
            choice.appendChild(analytics);
            choice.appendChild(details);

            const modelProb = Number.parseFloat((modelText || "").replace("%", "")) / 100;
            const marketProb = Number.parseFloat((marketText || "").replace("%", "")) / 100;
            const qualityLine = Array.from(choice.querySelectorAll(".check")).find((row) => row.textContent.includes("Data quality"));
            const qualityProb = qualityLine
                ? Number.parseFloat((qualityLine.textContent.match(/(\d+)%/) || ["", "0"])[1]) / 100
                : null;

            const modelPct = toPercent(modelProb);
            const marketPct = toPercent(marketProb);
            const qualityPct = toPercent(qualityProb);
            const modelNode = analytics.querySelector("[data-model-pct]");
            const marketNode = analytics.querySelector("[data-market-pct]");
            const qualityNode = analytics.querySelector("[data-quality-pct]");
            if (modelNode) modelNode.textContent = modelPct;
            if (marketNode) marketNode.textContent = marketPct;
            if (qualityNode) qualityNode.textContent = qualityPct;

            const modelFill = analytics.querySelector("[data-model-fill]");
            const marketFill = analytics.querySelector("[data-market-fill]");
            const qualityFill = analytics.querySelector("[data-quality-fill]");
            if (modelFill) modelFill.style.width = clampPercent(modelProb) + "%";
            if (marketFill) marketFill.style.width = clampPercent(marketProb) + "%";
            if (qualityFill) qualityFill.style.width = clampPercent(qualityProb) + "%";
        });
    }

    function wireCalculator(form) {
        const result = form.querySelector("[data-practice-output]");
        const error = form.querySelector("[data-practice-error]");

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (error) error.textContent = "";
            if (result) result.innerHTML = "";

            const payload = {
                american_odds: form.querySelector("[name='american_odds']").value,
                wager_amount_usd: form.querySelector("[name='wager_amount_usd']").value,
                model_probability: form.querySelector("[name='model_probability']").value,
                market_implied_probability: form.querySelector("[name='market_implied_probability']").value,
            };

            try {
                const response = await fetch("/api/sip/practice-wager", {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "Accept": "application/json" },
                    body: JSON.stringify(payload),
                });
                const body = await response.json();
                if (!response.ok) {
                    throw new Error(body.error || "Calculation failed.");
                }

                if (!result) {
                    return;
                }

                result.innerHTML = [
                    ["Potential profit", "$" + body.potential_profit_usd],
                    ["Total return", "$" + body.total_return_usd],
                    ["Break-even probability", Number(body.break_even_probability) * 100 + "%"],
                    ["Expected profit", body.expected_profit_usd === null ? "Unavailable" : "$" + body.expected_profit_usd],
                    [
                        "Expected return percentage",
                        body.expected_return_percentage === null ? "Unavailable" : body.expected_return_percentage + "%",
                    ],
                    ["Win outcome", "$" + body.win_outcome_usd],
                    ["Loss outcome", "$" + body.loss_outcome_usd],
                    ["Explanation", body.plain_language],
                ].map(function (row) {
                    return "<div><span>" + row[0] + "</span><strong>" + row[1] + "</strong></div>";
                }).join("");
            } catch (caught) {
                if (error) {
                    error.textContent = caught.message;
                }
            }
        });
    }

    function initCalculators() {
        document.querySelectorAll(".sipx-practice-form").forEach(wireCalculator);
    }

    function init() {
        addCardGuidance();
        initCalculators();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
