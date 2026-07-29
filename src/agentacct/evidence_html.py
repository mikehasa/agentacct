from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

from .agent_capabilities import agent_capability_manifest


ADVANCED_WORK_EVIDENCE_LIMIT = 100
ADVANCED_WORK_EVIDENCE_RECORD_LIMIT = 5


_SOURCE_LABELS = {
    "claude-code": "Claude Code",
    "client_hook": "Client hook",
    "codex": "Codex",
    "entire": "Entire",
    "mcp_agent_reported": "MCP agent report",
    "openlit": "OpenLIT",
    "otlp_http_json": "OTLP / HTTP JSON",
    "paperclip": "Paperclip",
    "provider_invoice": "Provider invoice",
}

_DISCREPANCY_TITLES = {
    "cost_basis_variance": "Cost sources use different bases",
    "cost_value_conflict": "Cost values disagree",
    "machine_check_value_conflict": "Machine-check results disagree",
    "outcome_value_conflict": "Outcome reports disagree",
    "semantic_context_missing": "Work meaning is missing",
    "source_identity_conflict": "A source event has conflicting versions",
    "usage_value_conflict": "Usage values disagree",
}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _short(value: Any, limit: int = 28) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:12]}…{text[-10:]}"


def _json_cell(value: Any) -> str:
    if value in (None, {}, [], ()):
        return '<span class="note">Missing</span>'
    return f"<code>{_esc(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))}</code>"


def _human(value: Any, *, fallback: str = "Unknown") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text.lower() in _SOURCE_LABELS:
        return _SOURCE_LABELS[text.lower()]
    return " ".join(
        part.capitalize()
        for part in text.replace("_", "-").replace(".", "-").split("-")
        if part
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _declared_total(container: Mapping[str, Any], key: str, actual: int) -> int:
    value = container.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= actual:
        return value
    return actual


def _cap_note(shown: int, total: int, noun: str) -> str:
    return f'<span class="note">Showing {_esc(shown)} of {_esc(total)} {_esc(noun)}.</span>'


def _flat_labels(value: Any) -> str:
    if isinstance(value, Mapping):
        return ", ".join(
            f"{_human(key)} ({child})" if isinstance(child, int) and not isinstance(child, bool) else _human(key)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        ) or "Missing"
    if isinstance(value, (list, tuple)):
        return ", ".join(_human(item) for item in value) or "Missing"
    return _human(value, fallback="Missing")


def _source_label(value: Any, *, source_type: Any = None) -> str:
    if isinstance(value, Mapping):
        system = value.get("source_system") or value.get("system") or value.get("name")
        kind = value.get("source_type") or value.get("type") or source_type
    else:
        system = value
        kind = source_type
    system_label = _human(system, fallback="Unknown source")
    kind_label = _human(kind, fallback="")
    return system_label if not kind_label or kind_label == system_label else f"{system_label} · {kind_label}"


def _connection_label(value: Any) -> str:
    return "Not verified" if value == "not_verified" else _human(value, fallback="Not verified")


def _time_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Missing"
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    return parsed.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _timestamp_sort_entry(value: Any) -> tuple[float, str]:
    text = str(value or "").strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return float("-inf"), text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return float("-inf"), text
    return parsed.timestamp(), text


def _completeness_label(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("status")
    return _human(value, fallback="Unknown")


def _metrics(summary: Mapping[str, Any]) -> str:
    metrics = (
        ("Evidence", summary.get("evidence_count", 0), "bridge"),
        ("Observed", summary.get("observed_count", 0), ""),
        ("Claimed", summary.get("claimed_count", 0), "log"),
        ("Sources", summary.get("source_count", 0), ""),
        ("Discrepancies", summary.get("discrepancy_count", 0), "warn"),
    )
    cards = "".join(
        f'<div class="metric {css}"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div></div>'
        for label, value, css in metrics
    )
    return f'<div class="metric-grid">{cards}</div>'


def _empty(message: str) -> str:
    return f'<div class="section"><div class="section-header"><h2>No evidence yet</h2></div><p class="section-note">{_esc(message)}</p></div>'


def render_source_coverage_compact(product: Mapping[str, Any]) -> str:
    """Render bounded, decision-friendly coverage without source-instance ids."""

    coverage = _mapping(product.get("source_coverage"))
    sources = _mapping_rows(coverage.get("sources"))
    visible = sources[:50]
    total = _declared_total(coverage, "source_count", len(sources))
    if not sources:
        return """
        <div class="section">
          <div class="section-header"><h2>Source coverage</h2></div>
          <p class="section-note">No source evidence has been indexed yet. This says nothing about whether a connector is installed or healthy.</p>
        </div>
        """
    cap_note = _cap_note(len(visible), total, "sources") if len(visible) < total else ""
    cards = "".join(
        '<article class="source-coverage-card">'
        '<div class="work-card-header">'
        f"<div><h3>{_esc(_source_label(source.get('source_system'), source_type=source.get('source_type')))}</h3>"
        f'<div class="source-coverage-meta">Last evidence received: {_esc(_time_label(source.get("last_observed_at") or source.get("last_event_at")))}</div></div>'
        f'<span class="status status-missing">{_esc(_connection_label(source.get("connection_state")))}</span>'
        "</div>"
        f'<div class="value">{_esc(source.get("evidence_count", 0))} evidence received</div>'
        f'<div class="source-coverage-meta">Covers {_esc(_flat_labels(source.get("dimensions")))}.</div>'
        '<div class="source-coverage-stats">'
        f'<span class="chip">{_esc(source.get("observed_count", 0))} observed</span>'
        f'<span class="chip">{_esc(source.get("claimed_count", 0))} claimed</span>'
        f'<span class="chip">{_esc(source.get("partial_count", 0))} partial</span>'
        "</div></article>"
        for source in visible
    )
    return f"""
    <div class="section">
      <div class="section-header"><h2>Source coverage</h2>{cap_note}</div>
      <p class="section-note">Evidence received means agentacct has saved records from this source. It does not prove that the source is installed, connected, or healthy now; connection remains “Not verified.”</p>
      <div class="source-coverage-grid">{cards}</div>
    </div>
    """


def _digest_label(digest: Mapping[str, Any]) -> str:
    match = _mapping(digest.get("match"))
    for key in ("display_label", "label", "title"):
        value = match.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("display_label", "match_label", "title", "label"):
        value = digest.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    kind = match.get("kind") or digest.get("match_kind") or digest.get("subject_kind") or digest.get("kind")
    return f"{_human(kind, fallback='Work')} evidence"


def _digest_summary(digest: Mapping[str, Any], records: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    summary = _mapping(digest.get("summary"))
    if summary:
        return summary
    observed = sum(1 for record in records if record.get("assertion") == "observed")
    claimed = sum(1 for record in records if record.get("assertion") == "claimed")
    return {
        "evidence_count": len(records),
        "observed_count": observed,
        "claimed_count": claimed,
    }


def _digest_latest_sort_key(digest: Mapping[str, Any]) -> tuple[float, str, str]:
    records = _mapping_rows(digest.get("records"))
    latest = max((_timestamp_sort_entry(record.get("time")) for record in records), default=(float("-inf"), ""))
    stable_id = str(digest.get("group_id") or digest.get("display_label") or _digest_label(digest))
    # Newest timestamp first; normalized source text and the opaque group id
    # are ascending deterministic tie-breaks.  Input/SQLite row order can
    # therefore never decide which match survives the render cap.
    return -latest[0], latest[1], stable_id


def _record_inspect_link(record: Mapping[str, Any]) -> str:
    evidence_id = record.get("evidence_id")
    if not isinstance(evidence_id, str) or not evidence_id:
        return '<span class="note">Unavailable</span>'
    href = "/evidence/events/" + quote(evidence_id, safe="")
    # The complete id exists only in the Advanced detail href. The visible link
    # deliberately has a human label so primary summaries never disclose it.
    return f'<a href="{_esc(href)}">Inspect record</a>'


def render_evidence_drawer(digest: Mapping[str, Any], *, record_limit: int = 100) -> str:
    """Render one closed native drawer over an allowlist of digest fields.

    Unknown fields (including payload/raw content) are intentionally ignored.
    Match ids and namespaces never enter the summary. Full evidence ids appear
    only URL-quoted in the Advanced detail's forensic inspector links.
    """

    if not isinstance(digest, Mapping):
        return ""
    records = _mapping_rows(digest.get("records"))
    summary = _digest_summary(digest, records)
    visible = records[:record_limit]
    total = _declared_total(summary, "evidence_count", len(records))
    observed = summary.get("observed_count", 0)
    claimed = summary.get("claimed_count", 0)
    summary_sources = _flat_labels(summary.get("sources"))
    summary_dimensions = _flat_labels(summary.get("dimensions"))
    rows = "".join(
        "<tr>"
        f"<td>{_esc(_source_label(record.get('source'), source_type=record.get('source_type')))}</td>"
        f"<td>{_esc(_human(record.get('assertion')))}</td>"
        f"<td>{_esc(_human(record.get('event_type')))}</td>"
        f"<td>{_esc(record.get('time') or 'Missing')}</td>"
        f"<td>{_esc(_flat_labels(record.get('dimensions')))}</td>"
        f"<td>{_esc(_flat_labels(record.get('basis')))}</td>"
        f"<td>{_esc(_flat_labels(record.get('authority')))}</td>"
        f"<td>{_esc(_completeness_label(record.get('completeness')))}</td>"
        f"<td>{_record_inspect_link(record)}</td>"
        "</tr>"
        for record in visible
    )
    cap_note = _cap_note(len(visible), total, "evidence records") if len(visible) < total else ""
    detail = (
        '<p class="section-note">No bounded evidence records are available for this match.</p>'
        if not visible
        else f"""
        <p class="section-note">Sources: {_esc(summary_sources)} · Dimensions: {_esc(summary_dimensions)}. Advanced detail shows normalized metadata only. Inspector links may contain opaque record identifiers; payload and raw source content are never embedded here.</p>
        <div class="table-wrap"><table><thead><tr><th>Source</th><th>Assertion</th><th>Event</th><th>Time</th><th>Dimensions</th><th>Basis</th><th>Authority</th><th>Completeness</th><th>Forensic detail</th></tr></thead><tbody>{rows}</tbody></table></div>
        {cap_note}
        """
    )
    return f"""
    <details class="evidence-drawer">
      <summary><strong>{_esc(_digest_label(digest))}</strong> <span class="note">{_esc(total)} evidence · {_esc(observed)} observed · {_esc(claimed)} claimed</span></summary>
      <div class="drawer-body">{detail}</div>
    </details>
    """


_CAPABILITY_STATE_LABELS = {
    "verified": "Verified path",
    "verified_partial": "Limited verified path",
    "experimental": "Experimental",
    "unavailable": "Unavailable",
}

_CAPABILITY_STATE_CLASSES = {
    "verified": "status-ready",
    "verified_partial": "status-needs-import",
    "experimental": "status-needs-import",
    "unavailable": "status-missing",
}

_ACTIVATION_LABELS = {
    "none": "No activation",
    "manual_manifest": "Manual manifest",
    "manual_profile": "Manual profile",
    "opt_in_project": "Opt-in project",
    "one_command_project": "One-command project",
}


def _capability_cell(capability: Mapping[str, Any]) -> str:
    state = str(capability.get("state") or "unavailable")
    verification = _mapping(capability.get("verification"))
    verification_level = _human(verification.get("level"), fallback="No verification")
    verified_at = str(verification.get("verified_at") or "")
    verification_note = verification_level + (f" · {verified_at}" if verified_at else "")
    activation = _ACTIVATION_LABELS.get(str(capability.get("activation") or "none"), "Unknown activation")
    limitations = [str(value) for value in capability.get("limitations", []) if str(value).strip()]
    evidence_refs = [str(value) for value in verification.get("evidence_refs", []) if str(value).strip()]
    versions = [str(value) for value in verification.get("client_versions", []) if str(value).strip()]
    bases = [
        f"Usage basis: {_human(capability.get('usage_basis'))}"
        for _unused in (None,)
        if capability.get("usage_basis") not in {None, "unknown"}
    ] + [
        f"Cost basis: {_human(capability.get('cost_basis'))}"
        for _unused in (None,)
        if capability.get("cost_basis") not in {None, "unknown"}
    ]
    first_limit = f'<span class="note">Limit: {_esc(limitations[0])}</span>' if limitations else ""
    detail_rows = [
        *(f"<li>{_esc(value)}</li>" for value in limitations[1:]),
        *(f"<li>{_esc(value)}</li>" for value in bases),
        *(f"<li>Client version: <code>{_esc(value)}</code></li>" for value in versions),
        *(f"<li>Evidence: <code>{_esc(value)}</code></li>" for value in evidence_refs),
    ]
    details = (
        '<details class="capability-detail"><summary>More evidence</summary><ul>'
        + "".join(detail_rows)
        + "</ul></details>"
        if detail_rows
        else ""
    )
    return (
        f'<span class="status {_CAPABILITY_STATE_CLASSES.get(state, "status-missing")}">'
        f'{_esc(_CAPABILITY_STATE_LABELS.get(state, "Unavailable"))}</span>'
        f'<small>{_esc(activation)} · {_esc(verification_note)}</small>'
        f'<span class="note">{_esc(capability.get("scope") or "No declared scope")}</span>'
        f"{first_limit}{details}"
    )


def _agent_identity_cell(row: Mapping[str, Any]) -> str:
    stability = _mapping(row.get("verified_stability"))
    stability_bits = [_human(stability.get("level"))]
    if stability.get("verified_at"):
        stability_bits.append(str(stability["verified_at"]))
    versions = [str(value) for value in stability.get("client_versions", []) if str(value).strip()]
    if versions:
        stability_bits.append("version " + ", ".join(versions))
    limitations = [
        str(value)
        for value in [*row.get("limitations", []), *stability.get("limitations", [])]
        if str(value).strip()
    ]
    details = (
        '<details class="capability-detail"><summary>Agent limits</summary><ul>'
        + "".join(f"<li>{_esc(value)}</li>" for value in limitations)
        + "</ul></details>"
        if limitations
        else ""
    )
    return (
        f'<td><strong>{_esc(row.get("display_name") or row.get("client"))}</strong>'
        f'<small>{_esc(_human(row.get("roadmap_phase"), fallback="Unscheduled"))}</small>'
        f'<small>Stability: {_esc(" · ".join(stability_bits))}</small>'
        f'<span class="note">{_esc(row.get("session_scope") or "No declared session scope")}</span>'
        f"{details}</td>"
    )


def _agent_capability_table(clients: list[Mapping[str, Any]]) -> str:
    rows = "".join(
        "<tr>"
        + _agent_identity_cell(row)
        + "".join(
            f"<td>{_capability_cell(_mapping(_mapping(row.get('capabilities')).get(name)))}</td>"
            for name in (
                "session_discovery",
                "usage_import",
                "mechanical_capture",
                "mcp_semantics",
                "model_attribution",
                "cache_read",
                "cache_write",
                "automatic_install",
            )
        )
        + "</tr>"
        for row in clients
    )
    return f'<div class="table-wrap"><table class="capability-table"><thead><tr><th>Agent</th><th>Session discovery</th><th>Usage import</th><th>Mechanical capture</th><th>MCP semantics</th><th>Model attribution</th><th>Cache read</th><th>Cache write</th><th>Automatic install</th></tr></thead><tbody>{rows}</tbody></table></div>'


def render_agent_capability_manifest_body(manifest: Mapping[str, Any]) -> str:
    """Render static adapter truth without implying current runtime health."""

    clients = _mapping_rows(manifest.get("clients"))
    current = [row for row in clients if row.get("roadmap_phase") != "phase_2"]
    roadmap = [row for row in clients if row.get("roadmap_phase") == "phase_2"]
    roadmap_table = (
        '<details class="capability-roadmap"><summary>Roadmap only — no implemented or verified adapter path</summary>'
        + _agent_capability_table(roadmap)
        + "</details>"
        if roadmap
        else ""
    )
    return f"""
    <div class="section" id="agent-capability-coverage">
      <div class="section-header"><h2>Agent capability coverage</h2><span class="note">Reviewed {_esc(manifest.get('last_reviewed_at') or 'Unknown')}</span></div>
      <p class="section-note">Each lane is judged independently. “Limited verified path” means only the written scope was verified; it is not a whole-agent support badge. Current files on this machine still come from <a href="/usage/sources">source detection</a>, and current importer health still comes from <a href="/ingestion/health">ingestion health</a>. Missing cache fields remain unknown, never zero.</p>
      {_agent_capability_table(current)}
      {roadmap_table}
      <p class="section-note"><a href="/capabilities/agents">Open machine-readable manifest →</a> · Runtime detection, latest health, and historical evidence remain separate truth surfaces.</p>
    </div>
    """


def render_advanced_index_body(
    product: Mapping[str, Any],
    *,
    capabilities: Mapping[str, Any] | None = None,
) -> str:
    """Render the explicit boundary between product views and inspectors."""

    work_evidence = _mapping(product.get("work_evidence"))
    digests = _mapping_rows(work_evidence.get("items"))
    digest_total = _declared_total(work_evidence, "item_count", len(digests))
    visible_digests = sorted(digests, key=_digest_latest_sort_key)[:ADVANCED_WORK_EVIDENCE_LIMIT]
    drawers = "".join(
        render_evidence_drawer(digest, record_limit=ADVANCED_WORK_EVIDENCE_RECORD_LIMIT)
        for digest in visible_digests
    )
    if not drawers:
        drawers = '<p class="section-note">No work-scoped evidence digests are available yet.</p>'
    status = _mapping(product.get("status"))
    stats = _mapping(status.get("stats"))
    summary = _mapping(product.get("summary"))
    projected_evidence = int(summary.get("evidence_count") or 0)
    indexed_evidence = int(stats.get("evidence_versions") or projected_evidence)
    projection_limit = int(status.get("limit") or projected_evidence)
    if status.get("scope") == "advanced_html_preview" and status.get("truncated") is True:
        projection_note = (
            f" Preview capped at {_esc(f'{projection_limit:,}')} latest evidence records; older indexed history "
            "remains available in the forensic inspectors."
        )
    elif status.get("truncated") is True and indexed_evidence > projected_evidence:
        projection_note = (
            f" This bounded projection contains {_esc(f'{projected_evidence:,}')} of "
            f"{_esc(f'{indexed_evidence:,}')} indexed evidence versions."
        )
    elif status.get("truncated") is True:
        projection_note = (
            f" This bounded projection contains the latest {_esc(f'{projected_evidence:,}')} indexed evidence "
            "versions; older evidence remains available in the forensic inspectors."
        )
    else:
        projection_note = ""
    digest_cap_note = (
        _cap_note(len(visible_digests), digest_total, "projected work matches")
        if len(visible_digests) < digest_total
        else ""
    )
    capability_manifest = agent_capability_manifest() if capabilities is None else capabilities
    return f"""
    <section class="work-intro"><div><h2>Inspect only when you need to</h2><p>The Work page is the normal starting point. Advanced keeps source previews, evidence projections, and record-level provenance available without making them the product's primary interface.</p></div></section>
    {render_agent_capability_manifest_body(capability_manifest)}
    <div class="section">
      <div class="section-header"><h2>Evidence projections</h2><span class="note">Bounded diagnostic views</span></div>
      <p class="section-note">Use these when a Work card's explanation is not enough. They preserve source and measurement boundaries and may expose technical vocabulary.</p>
      <div class="advanced-grid">
        <article class="advanced-card"><h3>Local log preview</h3><p>Inspect unsaved client-log discovery, raw normalized events, source paths, and import diagnostics.</p><a href="/raw">Open raw data →</a></article>
        <article class="advanced-card"><h3>Work graph</h3><p>Trace evidence-backed relationships among work, sessions, runs, tools, artifacts, and checks.</p><a href="/work-graph">Open work graph →</a></article>
        <article class="advanced-card"><h3>Evidence matrix</h3><p>Compare which sources cover usage, cost, lifecycle, outcomes, and machine checks.</p><a href="/evidence-matrix">Open evidence matrix →</a></article>
        <article class="advanced-card"><h3>Discrepancies</h3><p>Review conflicting values, missing semantic context, and source-identity conflicts without silently picking a winner.</p><a href="/discrepancies">Open discrepancies →</a></article>
        <article class="advanced-card"><h3>Cost and outcome basis</h3><p>See whether a number is estimated, client-reported, telemetry-reported, or provider-billed.</p><a href="/cost-outcome-basis">Open basis records →</a></article>
      </div>
    </div>
    <div class="section">
      <div class="section-header"><h2>Forensic inspectors</h2><span class="note">Advanced JSON and record-level provenance</span></div>
      <p class="section-note">These JSON endpoints can expose opaque ids and complete normalized metadata. They never include source payload bodies.</p>
      <div class="advanced-grid">
        <article class="advanced-card"><h3>Evidence records</h3><p>Page through normalized envelopes and receipt metadata in stable arrival order.</p><a href="/evidence/events">Inspect records →</a></article>
        <article class="advanced-card"><h3>Claimed links</h3><p>Inspect explicit relationships between agent claims and supporting observations.</p><a href="/evidence/claimed-links">Inspect claimed links →</a></article>
        <article class="advanced-card"><h3>Store status</h3><p>Check the Evidence v2 feature state, spool, projection, duplicate, and conflict counters.</p><a href="/evidence/status">Inspect store status →</a></article>
        <article class="advanced-card"><h3>Projection JSON</h3><p>Read the larger bounded forensic projection; its status says when indexed history is truncated.</p><a href="/evidence/product">Inspect projection JSON →</a></article>
      </div>
    </div>
    {render_source_coverage_compact(product)}
    <div class="section">
      <div class="section-header"><h2>Work evidence details</h2>{digest_cap_note}</div>
      <p class="section-note">Drawers are closed by default. When capped, the latest-evidenced matches inside this projection are shown first.{projection_note} Open one to inspect bounded normalized evidence, <a href="/evidence/events">page all evidence records</a>, or <a href="/evidence/product">inspect the bounded projection JSON</a>.</p>
      {drawers}
    </div>
    """


def render_work_graph_body(product: Mapping[str, Any]) -> str:
    summary = product.get("summary") if isinstance(product.get("summary"), Mapping) else {}
    graph = product.get("work_graph") if isinstance(product.get("work_graph"), Mapping) else {}
    nodes = _mapping_rows(graph.get("nodes"))
    edges = _mapping_rows(graph.get("edges"))
    if not nodes:
        detail = _empty("Enable a capture source or replay v1 events into Evidence v2. MCP semantics remain valuable but are not required for activity to appear.")
    else:
        visible_nodes = nodes[:200]
        visible_edges = edges[:300]
        node_total = _declared_total(graph, "node_count", len(nodes))
        edge_total = _declared_total(graph, "edge_count", len(edges))
        node_rows = "".join(
            "<tr>"
            f"<td>{_esc(node.get('kind'))}</td>"
            f"<td><code>{_esc(_short(node.get('value')))}</code></td>"
            f"<td>{_esc(node.get('evidence_count'))}</td>"
            "</tr>"
            for node in visible_nodes
        )
        edge_rows = "".join(
            "<tr>"
            f"<td><code>{_esc(_short(edge.get('from')))}</code></td>"
            f"<td>{_esc(edge.get('relation'))}</td>"
            f"<td><code>{_esc(_short(edge.get('to')))}</code></td>"
            f"<td>{_esc(edge.get('assertion'))}</td>"
            f"<td>{_esc(edge.get('link_confidence') or 'unknown')}</td>"
            f"<td>{_esc(', '.join(str(value) for value in edge.get('dimensions', [])))}</td>"
            "</tr>"
            for edge in visible_edges
        )
        detail = f"""
        <div class="section">
          <div class="section-header"><h2>Entity nodes</h2>{_cap_note(len(visible_nodes), node_total, 'entity nodes')}</div>
          <table><thead><tr><th>Kind</th><th>Identity</th><th>Evidence</th></tr></thead><tbody>{node_rows}</tbody></table>
        </div>
        <div class="section">
          <div class="section-header"><h2>Evidence-backed edges</h2>{_cap_note(len(visible_edges), edge_total, 'evidence-backed edges')}</div>
          <p class="section-note">A relationship remains a claim with provenance.</p>
          <table><thead><tr><th>From</th><th>Relation</th><th>To</th><th>Assertion</th><th>Link confidence</th><th>Dimensions</th></tr></thead><tbody>{edge_rows}</tbody></table>
        </div>
        """
    return f"""
    <div class="hero"><div><div class="eyebrow">Multi-source evidence</div><h1>Work Graph</h1><p class="subtitle">Work items, executions, client sessions, tools, artifacts, and checks connected only by inspectable evidence.</p></div></div>
    {_metrics(summary)}{detail}
    """


def render_evidence_matrix_body(product: Mapping[str, Any]) -> str:
    summary = product.get("summary") if isinstance(product.get("summary"), Mapping) else {}
    matrix = product.get("evidence_matrix") if isinstance(product.get("evidence_matrix"), Mapping) else {}
    rows = _mapping_rows(matrix.get("rows"))
    visible = rows[:500]
    total = _declared_total(matrix, "row_count", len(rows))
    body_rows = "".join(
        "<tr>"
        f"<td>{_esc(row.get('dimension'))}</td>"
        f"<td>{_esc(row.get('source_type'))}</td>"
        f"<td>{_esc(row.get('source_system'))}</td>"
        f"<td>{_esc(row.get('observed_count'))}</td>"
        f"<td>{_esc(row.get('claimed_count'))}</td>"
        f"<td>{_esc(row.get('complete_count'))} / {_esc(row.get('partial_count'))} / {_esc(row.get('unknown_count'))}</td>"
        f"<td>{_json_cell(row.get('authority_counts'))}</td>"
        f"<td>{_json_cell(row.get('measurement_bases'))}</td>"
        "</tr>"
        for row in visible
    )
    detail = (
        _empty("The matrix fills as hooks, local logs, OTLP, MCP, orchestrators, Git, CI, and provider records contribute evidence.")
        if not rows
        else f"""
        <div class="section">
          <div class="section-header"><h2>Source-by-dimension coverage</h2>{_cap_note(len(visible), total, 'source-dimension rows')}</div>
          <p class="section-note">Observed and claimed are never coalesced.</p>
          <table><thead><tr><th>Dimension</th><th>Source type</th><th>System</th><th>Observed</th><th>Claimed</th><th>Complete / partial / unknown</th><th>Authority</th><th>Measurement basis</th></tr></thead><tbody>{body_rows}</tbody></table>
        </div>
        """
    )
    return f"""
    <div class="hero"><div><div class="eyebrow">Multi-source evidence</div><h1>Evidence Matrix</h1><p class="subtitle">See which source supports each dimension, what it measured, and where coverage is partial or missing.</p></div></div>
    {_metrics(summary)}{detail}
    """


def render_discrepancies_body(product: Mapping[str, Any]) -> str:
    summary = product.get("summary") if isinstance(product.get("summary"), Mapping) else {}
    discrepancies = product.get("discrepancies") if isinstance(product.get("discrepancies"), Mapping) else {}
    items = _mapping_rows(discrepancies.get("items"))
    visible = items[:500]
    total = _declared_total(discrepancies, "count", len(items))
    rows = "".join(
        "<tr>"
        f"<td><span class=\"status {'status-error' if item.get('severity') == 'high' else 'status-needs-import' if item.get('severity') == 'medium' else 'status-missing'}\">{_esc(_human(item.get('severity')))}</span></td>"
        f"<td><strong>{_esc(item.get('title') or _DISCREPANCY_TITLES.get(str(item.get('kind')), _human(item.get('kind'), fallback='Evidence discrepancy')))}</strong></td>"
        f"<td>{_esc(item.get('explanation') or 'agentacct kept the evidence separate instead of choosing a side silently.')}</td>"
        f"<td>{_esc(_human(item.get('dimension'), fallback='Multiple'))}</td>"
        f"<td>{_esc(item.get('recommended_next_step') or 'Inspect the normalized evidence before acting.')}</td>"
        "</tr>"
        for item in visible
    )
    detail = (
        '<div class="notice">No discrepancies in the current indexed evidence. Missing sources may still mean coverage is incomplete.</div>'
        if not items
        else f"""
        <div class="section">
          <div class="section-header"><h2>Claims requiring attention</h2>{_cap_note(len(visible), total, 'discrepancies')}</div>
          <p class="section-note">agentacct preserves both sides instead of choosing silently. Opaque ids and candidate records stay in the Advanced forensic inspector.</p>
          <table><thead><tr><th>Severity</th><th>Issue</th><th>Why it matters</th><th>Dimension</th><th>Recommended next step</th></tr></thead><tbody>{rows}</tbody></table>
        </div>
        """
    )
    return f"""
    <div class="hero"><div><div class="eyebrow">Multi-source evidence</div><h1>Discrepancies</h1><p class="subtitle">Duplicate identities, conflicting values, missing semantics, and outcome mismatches remain explicit.</p></div></div>
    {_metrics(summary)}{detail}
    """


def render_cost_outcome_basis_body(product: Mapping[str, Any]) -> str:
    summary = product.get("summary") if isinstance(product.get("summary"), Mapping) else {}
    basis = product.get("cost_outcome_basis") if isinstance(product.get("cost_outcome_basis"), Mapping) else {}
    records = _mapping_rows(basis.get("records"))
    visible = records[:500]
    total = _declared_total(basis, "record_count", len(records))
    rows = "".join(
        "<tr>"
        f"<td>{_esc(record.get('dimension'))}</td>"
        f"<td>{_esc(record.get('source_system'))}<br><span class=\"note\">{_esc(record.get('source_type'))}</span></td>"
        f"<td>{_esc(record.get('assertion'))}</td>"
        f"<td>{_esc(record.get('measurement_basis') or 'unknown')}</td>"
        f"<td>{_esc(record.get('authority') or 'none')}</td>"
        f"<td>{_esc(record.get('completeness') or 'unknown')}</td>"
        f"<td>{_json_cell(record.get('values'))}</td>"
        f"<td><code>{_esc(_short(record.get('evidence_id')))}</code></td>"
        "</tr>"
        for record in visible
    )
    detail = (
        _empty("Cost, usage, machine-check, and outcome evidence will appear here with its measurement basis; unknown never becomes zero.")
        if not records
        else f"""
        <div class="section">
          <div class="section-header"><h2>Basis records</h2>{_cap_note(len(visible), total, 'basis records')}</div>
          <p class="section-note">Claims are not summed without non-overlapping scope evidence.</p>
          <table><thead><tr><th>Dimension</th><th>Source</th><th>Assertion</th><th>Basis</th><th>Authority</th><th>Completeness</th><th>Values</th><th>Evidence ID</th></tr></thead><tbody>{rows}</tbody></table>
        </div>
        """
    )
    return f"""
    <div class="hero"><div><div class="eyebrow">Multi-source evidence</div><h1>Cost &amp; Outcome Basis</h1><p class="subtitle">Inspect the source and measurement basis behind every cost, usage, and outcome statement.</p></div></div>
    {_metrics(summary)}{detail}
    """


__all__ = [
    "ADVANCED_WORK_EVIDENCE_LIMIT",
    "ADVANCED_WORK_EVIDENCE_RECORD_LIMIT",
    "render_advanced_index_body",
    "render_cost_outcome_basis_body",
    "render_discrepancies_body",
    "render_evidence_drawer",
    "render_evidence_matrix_body",
    "render_source_coverage_compact",
    "render_work_graph_body",
]
