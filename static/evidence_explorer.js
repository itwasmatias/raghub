(function () {
    "use strict";
    const root = document.getElementById("evidenceExplorer");
    const filter = document.getElementById("nodeFilterInput");
    let graph = {nodes: [], edges: []};

    function escapeHtml(value) {
        return String(value || "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
    }

    function render() {
        const term = filter.value.trim().toLowerCase();
        const nodes = graph.nodes.filter((item) => !term
            || (item.label + " " + item.node_type).toLowerCase().includes(term));
        const ids = new Set(nodes.map((item) => item.node_id));
        const edges = graph.edges.filter((item) => ids.has(item.source) && ids.has(item.target));
        document.getElementById("graphStats").textContent =
            nodes.length + " visible nodes · " + edges.length + " evidence-backed relationships";
        document.getElementById("nodeList").innerHTML = nodes.map((item) =>
            '<article class="node"><strong>' + escapeHtml(item.label)
            + '</strong><br><span class="muted">' + escapeHtml(item.node_type)
            + ' · Evidence confidence ' + Math.round(Number(item.score || 0))
            + '/100</span><details><summary>What this score means</summary><p>'
            + escapeHtml((item.score_definition || {}).meaning)
            + '<br>Factors: ' + escapeHtml(((item.score_definition || {}).main_contributing_factors || []).join(", "))
            + '</p></details></article>'
        ).join("") || '<article class="card">No non-zero nodes match this filter.</article>';
        document.getElementById("edgeList").innerHTML = edges.map((item) =>
            '<article class="edge"><strong>' + escapeHtml(item.relationship)
            + '</strong><p>' + escapeHtml(item.source) + ' → ' + escapeHtml(item.target)
            + '<br>Weight ' + Number(item.weight || 0).toFixed(2)
            + '<br>' + escapeHtml(item.evidence) + '</p></article>'
        ).join("") || '<article class="card">No evidence-backed relationships match this filter.</article>';
    }

    async function load() {
        const params = new URLSearchParams({focus: root.dataset.focus, q: root.dataset.query});
        const response = await fetch("/api/situation-room/graph?" + params.toString());
        graph = await response.json();
        render();
    }

    filter.addEventListener("input", render);
    document.getElementById("graphReloadButton").addEventListener("click", load);
    load();
}());
