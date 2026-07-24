-- Canonical SQLite truth model.
--
-- Two store roles share this schema: offline candidate databases built by the
-- verified importer, and the live store (`chronicle.sqlite3` inside a
-- Chronicle state root) written by the flag-gated live shadow runtime. The
-- role is pinned per store in store_metadata and enforced at open time.

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    app_version TEXT NOT NULL,
    applied_at_us INTEGER NOT NULL CHECK (applied_at_us >= 0)
) STRICT;

CREATE TABLE store_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_uuid TEXT NOT NULL UNIQUE,
    store_role TEXT NOT NULL CHECK (store_role IN ('candidate', 'live')),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    canonical_sequence INTEGER NOT NULL DEFAULT 0 CHECK (canonical_sequence >= 0),
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0)
) STRICT;

CREATE TABLE source_instances (
    source_instance_id INTEGER PRIMARY KEY,
    client TEXT NOT NULL CHECK (length(client) BETWEEN 1 AND 64),
    adapter TEXT NOT NULL CHECK (length(adapter) BETWEEN 1 AND 128),
    representation TEXT NOT NULL CHECK (length(representation) BETWEEN 1 AND 128),
    namespace_scheme TEXT NOT NULL CHECK (length(namespace_scheme) BETWEEN 1 AND 64),
    namespace_digest BLOB NOT NULL CHECK (length(namespace_digest) = 32),
    source_schema_version TEXT,
    privacy_label TEXT,
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
    UNIQUE (client, namespace_digest, representation)
) STRICT;

CREATE INDEX idx_source_instances_client
    ON source_instances(client, representation, source_instance_id);

CREATE TABLE source_revisions (
    source_revision_id INTEGER PRIMARY KEY,
    source_instance_id INTEGER NOT NULL REFERENCES source_instances(source_instance_id),
    native_entity_kind TEXT NOT NULL CHECK (length(native_entity_kind) BETWEEN 1 AND 64),
    native_entity_key TEXT NOT NULL CHECK (length(native_entity_key) BETWEEN 1 AND 512),
    native_order INTEGER,
    revision_digest BLOB NOT NULL CHECK (length(revision_digest) = 32),
    content_hash BLOB NOT NULL CHECK (length(content_hash) = 32),
    parse_status TEXT NOT NULL CHECK (parse_status IN ('complete', 'partial', 'incompatible', 'invalid')),
    observed_at_us INTEGER NOT NULL CHECK (observed_at_us >= 0),
    UNIQUE (source_instance_id, native_entity_kind, native_entity_key, revision_digest)
) STRICT;

CREATE INDEX idx_source_revisions_entity_order
    ON source_revisions(source_instance_id, native_entity_kind, native_entity_key, native_order DESC);

CREATE TABLE source_conflicts (
    source_conflict_id INTEGER PRIMARY KEY,
    source_instance_id INTEGER NOT NULL REFERENCES source_instances(source_instance_id),
    native_entity_kind TEXT NOT NULL CHECK (length(native_entity_kind) BETWEEN 1 AND 64),
    native_entity_key TEXT NOT NULL CHECK (length(native_entity_key) BETWEEN 1 AND 512),
    incumbent_hash BLOB NOT NULL CHECK (length(incumbent_hash) = 32),
    incoming_hash BLOB NOT NULL CHECK (length(incoming_hash) = 32),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 128),
    first_seen_at_us INTEGER NOT NULL CHECK (first_seen_at_us >= 0),
    last_seen_at_us INTEGER NOT NULL CHECK (last_seen_at_us >= first_seen_at_us),
    seen_count INTEGER NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
    resolution_state TEXT NOT NULL DEFAULT 'open' CHECK (resolution_state IN ('open', 'resolved', 'ignored')),
    UNIQUE (
        source_instance_id,
        native_entity_kind,
        native_entity_key,
        incumbent_hash,
        incoming_hash,
        reason
    )
) STRICT;

CREATE INDEX idx_source_conflicts_open
    ON source_conflicts(resolution_state, last_seen_at_us DESC, source_conflict_id DESC);

CREATE TABLE sessions (
    session_id INTEGER PRIMARY KEY,
    source_instance_id INTEGER NOT NULL REFERENCES source_instances(source_instance_id),
    client_session_id TEXT NOT NULL CHECK (length(client_session_id) BETWEEN 1 AND 512),
    session_kind TEXT NOT NULL DEFAULT 'root' CHECK (session_kind IN ('root', 'child', 'internal', 'continuation', 'retry', 'unknown')),
    started_at_us INTEGER CHECK (started_at_us IS NULL OR started_at_us >= 0),
    last_activity_at_us INTEGER CHECK (last_activity_at_us IS NULL OR last_activity_at_us >= 0),
    sort_activity_at_us INTEGER NOT NULL DEFAULT 0 CHECK (sort_activity_at_us >= 0),
    observation_order INTEGER,
    observation_hash BLOB CHECK (observation_hash IS NULL OR length(observation_hash) = 32),
    source_revision_id INTEGER REFERENCES source_revisions(source_revision_id),
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
    updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= created_at_us),
    UNIQUE (source_instance_id, client_session_id)
) STRICT;

CREATE INDEX idx_sessions_recent
    ON sessions(sort_activity_at_us DESC, session_id DESC);
CREATE INDEX idx_sessions_source_recent
    ON sessions(source_instance_id, sort_activity_at_us DESC, session_id DESC);

CREATE TABLE session_edges (
    session_edge_id INTEGER PRIMARY KEY,
    child_session_id INTEGER NOT NULL REFERENCES sessions(session_id),
    parent_session_id INTEGER NOT NULL REFERENCES sessions(session_id),
    relation TEXT NOT NULL CHECK (relation IN ('parent', 'root', 'continuation', 'retry')),
    basis TEXT NOT NULL CHECK (length(basis) BETWEEN 1 AND 128),
    validation_state TEXT NOT NULL CHECK (validation_state IN ('valid', 'rejected', 'conflict', 'pending')),
    veto_reason TEXT CHECK (veto_reason IS NULL OR length(veto_reason) BETWEEN 1 AND 128),
    source_revision_id INTEGER REFERENCES source_revisions(source_revision_id),
    content_hash BLOB NOT NULL CHECK (length(content_hash) = 32),
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
    CHECK (child_session_id <> parent_session_id),
    UNIQUE (child_session_id, parent_session_id, relation, basis)
) STRICT;

CREATE INDEX idx_session_edges_parent
    ON session_edges(parent_session_id, validation_state, relation, child_session_id);
CREATE INDEX idx_session_edges_child
    ON session_edges(child_session_id, validation_state, relation, parent_session_id);

-- Observation-level absorb state for one session: the last-wins raw parent
-- the source claimed, BEFORE edge validation. session_edges records the
-- validated lineage graph; this row is what an incremental writer needs to
-- merge a new observation the way the bulk importer's absorb does.
CREATE TABLE session_observed_lineage (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(session_id),
    parent_client TEXT NOT NULL CHECK (length(parent_client) BETWEEN 1 AND 64),
    parent_client_session_id TEXT NOT NULL CHECK (length(parent_client_session_id) BETWEEN 1 AND 512),
    parent_namespace_digest BLOB NOT NULL CHECK (length(parent_namespace_digest) = 32),
    updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= 0)
) STRICT;
CREATE UNIQUE INDEX uq_session_edges_one_valid_parent
    ON session_edges(child_session_id)
    WHERE validation_state = 'valid';

CREATE TABLE task_anchors (
    task_anchor_id INTEGER PRIMARY KEY,
    public_task_id TEXT NOT NULL UNIQUE,
    primary_session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(session_id),
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
    CHECK (
        length(public_task_id) = 37
        AND substr(public_task_id, 1, 5) = 'task_'
        AND substr(public_task_id, 6) NOT GLOB '*[^0-9a-f]*'
    )
) STRICT;

CREATE INDEX idx_task_anchors_primary
    ON task_anchors(primary_session_id, task_anchor_id);

CREATE TABLE task_aliases (
    task_alias_id INTEGER PRIMARY KEY,
    task_anchor_id INTEGER NOT NULL REFERENCES task_anchors(task_anchor_id) ON DELETE CASCADE,
    alias_scheme TEXT NOT NULL CHECK (length(alias_scheme) BETWEEN 1 AND 64),
    alias_value TEXT NOT NULL CHECK (length(alias_value) BETWEEN 1 AND 512),
    resolution_basis TEXT NOT NULL CHECK (length(resolution_basis) BETWEEN 1 AND 128),
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
    UNIQUE (alias_scheme, alias_value)
) STRICT;

CREATE TABLE facts (
    fact_id INTEGER PRIMARY KEY,
    source_instance_id INTEGER NOT NULL REFERENCES source_instances(source_instance_id),
    source_event_id TEXT,
    idempotency_scope TEXT,
    idempotency_key TEXT,
    fact_type TEXT NOT NULL CHECK (fact_type IN ('work_claim', 'machine_check', 'artifact', 'finding_action', 'mechanical_observation')),
    transport TEXT NOT NULL CHECK (length(transport) BETWEEN 1 AND 64),
    strength TEXT NOT NULL CHECK (strength IN ('recorded_claim', 'mechanical_observation', 'machine_check', 'inspectable_artifact')),
    occurred_at_us INTEGER NOT NULL CHECK (occurred_at_us >= 0),
    source_order INTEGER,
    content_hash BLOB NOT NULL CHECK (length(content_hash) = 32),
    supersedes_fact_id INTEGER REFERENCES facts(fact_id),
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
    CHECK ((idempotency_scope IS NULL) = (idempotency_key IS NULL)),
    CHECK (supersedes_fact_id IS NULL OR supersedes_fact_id <> fact_id)
) STRICT;

CREATE UNIQUE INDEX uq_facts_source_event
    ON facts(source_instance_id, source_event_id)
    WHERE source_event_id IS NOT NULL;
CREATE UNIQUE INDEX uq_facts_idempotency
    ON facts(source_instance_id, idempotency_scope, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_facts_time
    ON facts(occurred_at_us DESC, fact_id DESC);
CREATE INDEX idx_facts_supersedes
    ON facts(supersedes_fact_id, fact_id);
CREATE UNIQUE INDEX uq_facts_single_correction
    ON facts(supersedes_fact_id)
    WHERE supersedes_fact_id IS NOT NULL;

CREATE TABLE work_claims (
    fact_id INTEGER PRIMARY KEY REFERENCES facts(fact_id) ON DELETE CASCADE,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('task', 'section', 'checkpoint', 'completion', 'blocker', 'event')),
    status TEXT CHECK (status IS NULL OR status IN ('started', 'checkpoint', 'completed', 'blocked', 'reported', 'aborted')),
    title TEXT CHECK (title IS NULL OR length(title) <= 512),
    objective TEXT CHECK (objective IS NULL OR length(objective) <= 4096),
    summary TEXT CHECK (summary IS NULL OR length(summary) <= 8192),
    blocker TEXT CHECK (blocker IS NULL OR length(blocker) <= 4096),
    next_step TEXT CHECK (next_step IS NULL OR length(next_step) <= 4096),
    work_id TEXT CHECK (work_id IS NULL OR length(work_id) <= 256),
    section_id TEXT CHECK (section_id IS NULL OR length(section_id) <= 256),
    outcome_axis TEXT NOT NULL CHECK (outcome_axis IN ('intent', 'execution', 'verification', 'outcome', 'finding'))
) STRICT;

CREATE TABLE claim_scopes (
    claim_scope_id INTEGER PRIMARY KEY,
    scope_key TEXT NOT NULL UNIQUE CHECK (length(scope_key) BETWEEN 1 AND 512),
    scope_kind TEXT NOT NULL CHECK (length(scope_kind) BETWEEN 1 AND 64)
) STRICT;

CREATE TABLE scope_revisions (
    scope_revision_id INTEGER PRIMARY KEY,
    claim_scope_id INTEGER NOT NULL REFERENCES claim_scopes(claim_scope_id),
    revision_key TEXT NOT NULL CHECK (length(revision_key) BETWEEN 1 AND 512),
    revision_order INTEGER,
    revision_digest BLOB CHECK (revision_digest IS NULL OR length(revision_digest) = 32),
    UNIQUE (claim_scope_id, revision_key),
    UNIQUE (claim_scope_id, scope_revision_id)
) STRICT;

CREATE TABLE machine_checks (
    fact_id INTEGER PRIMARY KEY REFERENCES facts(fact_id) ON DELETE CASCADE,
    claim_scope_id INTEGER NOT NULL REFERENCES claim_scopes(claim_scope_id),
    scope_revision_id INTEGER,
    check_identity TEXT NOT NULL CHECK (length(check_identity) BETWEEN 1 AND 256),
    evidence_type TEXT NOT NULL CHECK (length(evidence_type) BETWEEN 1 AND 64),
    result TEXT NOT NULL CHECK (result IN ('passed', 'failed', 'skipped', 'error', 'unknown')),
    exit_code INTEGER,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    command_digest BLOB CHECK (command_digest IS NULL OR length(command_digest) = 32),
    command_display TEXT CHECK (command_display IS NULL OR length(command_display) <= 1024),
    FOREIGN KEY (claim_scope_id, scope_revision_id)
        REFERENCES scope_revisions(claim_scope_id, scope_revision_id)
) STRICT;

CREATE INDEX idx_machine_checks_scope
    ON machine_checks(claim_scope_id, scope_revision_id, result, fact_id DESC);

CREATE TABLE artifacts (
    fact_id INTEGER PRIMARY KEY REFERENCES facts(fact_id) ON DELETE CASCADE,
    logical_name TEXT NOT NULL CHECK (length(logical_name) BETWEEN 1 AND 512),
    safe_reference TEXT CHECK (safe_reference IS NULL OR length(safe_reference) <= 2048),
    digest BLOB CHECK (digest IS NULL OR length(digest) = 32),
    availability TEXT NOT NULL CHECK (availability IN ('available', 'missing', 'unknown'))
) STRICT;

CREATE TABLE terminal_requirements (
    terminal_fact_id INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    claim_scope_id INTEGER NOT NULL REFERENCES claim_scopes(claim_scope_id),
    scope_revision_id INTEGER,
    PRIMARY KEY (terminal_fact_id, claim_scope_id),
    FOREIGN KEY (claim_scope_id, scope_revision_id)
        REFERENCES scope_revisions(claim_scope_id, scope_revision_id)
) STRICT;

CREATE TABLE finding_actions (
    fact_id INTEGER PRIMARY KEY REFERENCES facts(fact_id) ON DELETE CASCADE,
    finding_key TEXT NOT NULL CHECK (length(finding_key) BETWEEN 1 AND 512),
    action TEXT NOT NULL CHECK (action IN ('opened', 'resolved', 'reopened', 'dismissed')),
    expected_revision TEXT,
    blocker INTEGER NOT NULL CHECK (blocker IN (0, 1))
) STRICT;

CREATE TABLE usage_measurements (
    usage_measurement_id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(session_id),
    lane TEXT NOT NULL CHECK (length(lane) BETWEEN 1 AND 128),
    representation TEXT NOT NULL CHECK (length(representation) BETWEEN 1 AND 128),
    update_semantics TEXT NOT NULL CHECK (update_semantics = 'cumulative_snapshot'),
    precedence_role TEXT NOT NULL CHECK (precedence_role IN ('authoritative', 'fallback', 'enrichment')),
    granularity TEXT NOT NULL CHECK (granularity IN ('request', 'turn', 'session', 'task_only', 'unavailable')),
    totals_eligible INTEGER NOT NULL CHECK (totals_eligible IN (0, 1)),
    provider TEXT,
    model TEXT,
    usage_confidence TEXT NOT NULL CHECK (length(usage_confidence) BETWEEN 1 AND 64),
    held_reason TEXT,
    input_tokens INTEGER,
    input_tokens_reported INTEGER NOT NULL CHECK (input_tokens_reported IN (0, 1)),
    output_tokens INTEGER,
    output_tokens_reported INTEGER NOT NULL CHECK (output_tokens_reported IN (0, 1)),
    cached_input_tokens INTEGER,
    cached_input_tokens_reported INTEGER NOT NULL CHECK (cached_input_tokens_reported IN (0, 1)),
    cache_creation_input_tokens INTEGER,
    cache_creation_input_tokens_reported INTEGER NOT NULL CHECK (cache_creation_input_tokens_reported IN (0, 1)),
    cache_read_input_tokens INTEGER,
    cache_read_input_tokens_reported INTEGER NOT NULL CHECK (cache_read_input_tokens_reported IN (0, 1)),
    reasoning_output_tokens INTEGER,
    reasoning_output_tokens_reported INTEGER NOT NULL CHECK (reasoning_output_tokens_reported IN (0, 1)),
    total_tokens INTEGER,
    total_tokens_reported INTEGER NOT NULL CHECK (total_tokens_reported IN (0, 1)),
    client_reported_cost_microusd INTEGER CHECK (client_reported_cost_microusd IS NULL OR client_reported_cost_microusd >= 0),
    source_order INTEGER,
    source_revision_id INTEGER REFERENCES source_revisions(source_revision_id),
    content_hash BLOB NOT NULL CHECK (length(content_hash) = 32),
    observed_at_us INTEGER NOT NULL CHECK (observed_at_us >= 0),
    updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= observed_at_us),
    CHECK ((input_tokens_reported = 0 AND input_tokens IS NULL) OR (input_tokens_reported = 1 AND input_tokens IS NOT NULL AND input_tokens >= 0)),
    CHECK ((output_tokens_reported = 0 AND output_tokens IS NULL) OR (output_tokens_reported = 1 AND output_tokens IS NOT NULL AND output_tokens >= 0)),
    CHECK ((cached_input_tokens_reported = 0 AND cached_input_tokens IS NULL) OR (cached_input_tokens_reported = 1 AND cached_input_tokens IS NOT NULL AND cached_input_tokens >= 0)),
    CHECK ((cache_creation_input_tokens_reported = 0 AND cache_creation_input_tokens IS NULL) OR (cache_creation_input_tokens_reported = 1 AND cache_creation_input_tokens IS NOT NULL AND cache_creation_input_tokens >= 0)),
    CHECK ((cache_read_input_tokens_reported = 0 AND cache_read_input_tokens IS NULL) OR (cache_read_input_tokens_reported = 1 AND cache_read_input_tokens IS NOT NULL AND cache_read_input_tokens >= 0)),
    CHECK ((reasoning_output_tokens_reported = 0 AND reasoning_output_tokens IS NULL) OR (reasoning_output_tokens_reported = 1 AND reasoning_output_tokens IS NOT NULL AND reasoning_output_tokens >= 0)),
    CHECK ((total_tokens_reported = 0 AND total_tokens IS NULL) OR (total_tokens_reported = 1 AND total_tokens IS NOT NULL AND total_tokens >= 0)),
    CHECK ((totals_eligible = 1 AND precedence_role IN ('authoritative', 'fallback')) OR totals_eligible = 0),
    CHECK ((totals_eligible = 1 AND held_reason IS NULL) OR totals_eligible = 0),
    UNIQUE (session_id, lane, representation)
) STRICT;

CREATE UNIQUE INDEX uq_usage_totals_lane
    ON usage_measurements(session_id, lane)
    WHERE totals_eligible = 1;
CREATE INDEX idx_usage_measurements_session
    ON usage_measurements(session_id, totals_eligible DESC, usage_measurement_id);
CREATE INDEX idx_usage_measurements_updated
    ON usage_measurements(updated_at_us DESC, usage_measurement_id DESC);
CREATE INDEX idx_usage_measurements_model
    ON usage_measurements(model, updated_at_us DESC, usage_measurement_id DESC);

CREATE TABLE usage_atomic_events (
    usage_atomic_event_id INTEGER PRIMARY KEY,
    source_instance_id INTEGER NOT NULL REFERENCES source_instances(source_instance_id),
    session_id INTEGER NOT NULL REFERENCES sessions(session_id),
    source_event_id TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (length(lane) BETWEEN 1 AND 128),
    provider TEXT,
    model TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    reasoning_output_tokens INTEGER CHECK (reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0),
    content_hash BLOB NOT NULL CHECK (length(content_hash) = 32),
    occurred_at_us INTEGER NOT NULL CHECK (occurred_at_us >= 0),
    UNIQUE (source_instance_id, source_event_id)
) STRICT;

CREATE TABLE usage_checkpoints (
    usage_checkpoint_id INTEGER PRIMARY KEY,
    usage_measurement_id INTEGER NOT NULL REFERENCES usage_measurements(usage_measurement_id),
    boundary_kind TEXT NOT NULL CHECK (boundary_kind IN ('terminal', 'day', 'rotation', 'correction', 'migration')),
    boundary_key TEXT NOT NULL CHECK (length(boundary_key) BETWEEN 1 AND 256),
    content_hash BLOB NOT NULL CHECK (length(content_hash) = 32),
    lane TEXT NOT NULL CHECK (length(lane) BETWEEN 1 AND 128),
    representation TEXT NOT NULL CHECK (length(representation) BETWEEN 1 AND 128),
    precedence_role TEXT NOT NULL CHECK (precedence_role IN ('authoritative', 'fallback', 'enrichment')),
    provider TEXT,
    model TEXT,
    usage_confidence TEXT NOT NULL CHECK (length(usage_confidence) BETWEEN 1 AND 64),
    totals_eligible INTEGER NOT NULL CHECK (totals_eligible IN (0, 1)),
    held_reason TEXT,
    input_tokens INTEGER,
    input_tokens_reported INTEGER NOT NULL CHECK (input_tokens_reported IN (0, 1)),
    output_tokens INTEGER,
    output_tokens_reported INTEGER NOT NULL CHECK (output_tokens_reported IN (0, 1)),
    cached_input_tokens INTEGER,
    cached_input_tokens_reported INTEGER NOT NULL CHECK (cached_input_tokens_reported IN (0, 1)),
    cache_creation_input_tokens INTEGER,
    cache_creation_input_tokens_reported INTEGER NOT NULL CHECK (cache_creation_input_tokens_reported IN (0, 1)),
    cache_read_input_tokens INTEGER,
    cache_read_input_tokens_reported INTEGER NOT NULL CHECK (cache_read_input_tokens_reported IN (0, 1)),
    reasoning_output_tokens INTEGER,
    reasoning_output_tokens_reported INTEGER NOT NULL CHECK (reasoning_output_tokens_reported IN (0, 1)),
    total_tokens INTEGER,
    total_tokens_reported INTEGER NOT NULL CHECK (total_tokens_reported IN (0, 1)),
    client_reported_cost_microusd INTEGER CHECK (client_reported_cost_microusd IS NULL OR client_reported_cost_microusd >= 0),
    observed_at_us INTEGER NOT NULL CHECK (observed_at_us >= 0),
    updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= observed_at_us),
    created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
    CHECK (
        boundary_kind <> 'day'
        OR (
            length(boundary_key) = 10
            AND boundary_key GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        )
    ),
    CHECK ((input_tokens_reported = 0 AND input_tokens IS NULL) OR (input_tokens_reported = 1 AND input_tokens IS NOT NULL AND input_tokens >= 0)),
    CHECK ((output_tokens_reported = 0 AND output_tokens IS NULL) OR (output_tokens_reported = 1 AND output_tokens IS NOT NULL AND output_tokens >= 0)),
    CHECK ((cached_input_tokens_reported = 0 AND cached_input_tokens IS NULL) OR (cached_input_tokens_reported = 1 AND cached_input_tokens IS NOT NULL AND cached_input_tokens >= 0)),
    CHECK ((cache_creation_input_tokens_reported = 0 AND cache_creation_input_tokens IS NULL) OR (cache_creation_input_tokens_reported = 1 AND cache_creation_input_tokens IS NOT NULL AND cache_creation_input_tokens >= 0)),
    CHECK ((cache_read_input_tokens_reported = 0 AND cache_read_input_tokens IS NULL) OR (cache_read_input_tokens_reported = 1 AND cache_read_input_tokens IS NOT NULL AND cache_read_input_tokens >= 0)),
    CHECK ((reasoning_output_tokens_reported = 0 AND reasoning_output_tokens IS NULL) OR (reasoning_output_tokens_reported = 1 AND reasoning_output_tokens IS NOT NULL AND reasoning_output_tokens >= 0)),
    CHECK ((total_tokens_reported = 0 AND total_tokens IS NULL) OR (total_tokens_reported = 1 AND total_tokens IS NOT NULL AND total_tokens >= 0)),
    UNIQUE (usage_measurement_id, boundary_kind, boundary_key, content_hash)
) STRICT;

CREATE INDEX idx_usage_checkpoints_measurement
    ON usage_checkpoints(usage_measurement_id, created_at_us DESC, usage_checkpoint_id DESC);

CREATE TABLE cost_calculations (
    cost_calculation_id INTEGER PRIMARY KEY,
    usage_measurement_id INTEGER NOT NULL REFERENCES usage_measurements(usage_measurement_id),
    usage_content_hash BLOB NOT NULL CHECK (length(usage_content_hash) = 32),
    price_source TEXT NOT NULL CHECK (length(price_source) BETWEEN 1 AND 128),
    price_version TEXT NOT NULL CHECK (length(price_version) BETWEEN 1 AND 128),
    price_effective_at_us INTEGER NOT NULL CHECK (price_effective_at_us >= 0),
    formula_version TEXT NOT NULL CHECK (length(formula_version) BETWEEN 1 AND 128),
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    completeness TEXT NOT NULL CHECK (completeness IN ('complete', 'partial', 'unavailable')),
    result_microusd INTEGER CHECK (result_microusd IS NULL OR result_microusd >= 0),
    calculated_at_us INTEGER NOT NULL CHECK (calculated_at_us >= 0),
    CHECK (
        (completeness = 'unavailable' AND result_microusd IS NULL)
        OR (completeness IN ('complete', 'partial') AND result_microusd IS NOT NULL)
    ),
    UNIQUE (usage_content_hash, price_source, price_version, price_effective_at_us, formula_version, currency)
) STRICT;

CREATE INDEX idx_cost_calculations_usage
    ON cost_calculations(usage_measurement_id, calculated_at_us DESC, cost_calculation_id DESC);

CREATE TABLE fact_session_links (
    fact_session_link_id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES sessions(session_id),
    method TEXT NOT NULL CHECK (length(method) BETWEEN 1 AND 128),
    confidence TEXT NOT NULL CHECK (confidence IN ('exact', 'high', 'medium', 'low', 'ambiguous', 'rejected', 'conflict')),
    rule_version TEXT NOT NULL CHECK (length(rule_version) BETWEEN 1 AND 128),
    validation_state TEXT NOT NULL CHECK (validation_state IN ('valid', 'pending', 'rejected', 'conflict')),
    veto_reason TEXT,
    UNIQUE (fact_id, session_id, method, rule_version)
) STRICT;

CREATE INDEX idx_fact_session_links_session
    ON fact_session_links(session_id, validation_state, confidence, fact_id);

CREATE TABLE usage_fact_links (
    usage_fact_link_id INTEGER PRIMARY KEY,
    usage_measurement_id INTEGER NOT NULL REFERENCES usage_measurements(usage_measurement_id) ON DELETE CASCADE,
    fact_id INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    exact_identity_basis TEXT NOT NULL CHECK (length(exact_identity_basis) BETWEEN 1 AND 128),
    rule_version TEXT NOT NULL CHECK (length(rule_version) BETWEEN 1 AND 128),
    UNIQUE (usage_measurement_id, fact_id, exact_identity_basis, rule_version)
) STRICT;

CREATE TABLE evidence_support_links (
    evidence_support_link_id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    claim_scope_id INTEGER NOT NULL REFERENCES claim_scopes(claim_scope_id),
    scope_revision_id INTEGER,
    support_kind TEXT NOT NULL CHECK (support_kind IN ('supports', 'contradicts', 'supersedes')),
    FOREIGN KEY (claim_scope_id, scope_revision_id)
        REFERENCES scope_revisions(claim_scope_id, scope_revision_id)
) STRICT;

CREATE UNIQUE INDEX uq_evidence_support_links_scope
    ON evidence_support_links(fact_id, claim_scope_id, support_kind)
    WHERE scope_revision_id IS NULL;
CREATE UNIQUE INDEX uq_evidence_support_links_revision
    ON evidence_support_links(fact_id, claim_scope_id, scope_revision_id, support_kind)
    WHERE scope_revision_id IS NOT NULL;

CREATE TABLE source_health_current (
    source_instance_id INTEGER PRIMARY KEY REFERENCES source_instances(source_instance_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('healthy', 'partial', 'incompatible', 'failed', 'unknown')),
    last_attempt_at_us INTEGER CHECK (last_attempt_at_us IS NULL OR last_attempt_at_us >= 0),
    last_success_at_us INTEGER CHECK (last_success_at_us IS NULL OR last_success_at_us >= 0),
    reason_code TEXT,
    bounded_error TEXT CHECK (bounded_error IS NULL OR length(bounded_error) <= 2048)
) STRICT;

CREATE TABLE migration_issues (
    migration_issue_id INTEGER PRIMARY KEY,
    legacy_origin TEXT NOT NULL CHECK (length(legacy_origin) BETWEEN 1 AND 512),
    location_digest BLOB NOT NULL CHECK (length(location_digest) = 32),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 128),
    disposition TEXT NOT NULL CHECK (disposition IN ('excluded', 'quarantined', 'invalid', 'requires_choice')),
    count INTEGER NOT NULL DEFAULT 1 CHECK (count >= 1),
    first_seen_at_us INTEGER NOT NULL CHECK (first_seen_at_us >= 0),
    UNIQUE (legacy_origin, location_digest, reason, disposition)
) STRICT;

CREATE TABLE projection_generations (
    projection_name TEXT PRIMARY KEY,
    projection_version INTEGER NOT NULL CHECK (projection_version >= 1),
    built_through_sequence INTEGER NOT NULL CHECK (built_through_sequence >= 0),
    input_fingerprint BLOB NOT NULL CHECK (length(input_fingerprint) = 32),
    state TEXT NOT NULL CHECK (state IN ('current', 'stale', 'rebuilding', 'failed')),
    built_at_us INTEGER NOT NULL CHECK (built_at_us >= 0)
) STRICT;

CREATE TABLE rm_task_current (
    public_task_id TEXT PRIMARY KEY,
    task_anchor_id INTEGER NOT NULL UNIQUE REFERENCES task_anchors(task_anchor_id) ON DELETE CASCADE,
    primary_session_id INTEGER NOT NULL REFERENCES sessions(session_id),
    title TEXT CHECK (title IS NULL OR length(title) <= 512),
    projected_state TEXT NOT NULL CHECK (projected_state IN ('active', 'reported_complete', 'verified_complete', 'partial_needs_review', 'aborted', 'reopened')),
    last_activity_at_us INTEGER CHECK (last_activity_at_us IS NULL OR last_activity_at_us >= 0),
    sort_activity_at_us INTEGER NOT NULL CHECK (sort_activity_at_us >= 0),
    session_count INTEGER NOT NULL CHECK (session_count >= 1),
    usage_measurement_count INTEGER NOT NULL CHECK (usage_measurement_count >= 0),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
    usage_missing_field_count INTEGER NOT NULL CHECK (usage_missing_field_count >= 0),
    finding_count INTEGER NOT NULL DEFAULT 0 CHECK (finding_count >= 0),
    built_through_sequence INTEGER NOT NULL CHECK (built_through_sequence >= 0)
) STRICT;

CREATE INDEX idx_rm_task_work_recent
    ON rm_task_current(sort_activity_at_us DESC, public_task_id DESC);
CREATE INDEX idx_rm_task_state_recent
    ON rm_task_current(projected_state, sort_activity_at_us DESC, public_task_id DESC);

CREATE TABLE rm_usage_day (
    rm_usage_day_id INTEGER PRIMARY KEY,
    day TEXT NOT NULL CHECK (length(day) = 10),
    client TEXT NOT NULL CHECK (length(client) BETWEEN 1 AND 64),
    provider TEXT,
    model TEXT,
    usage_basis TEXT NOT NULL CHECK (length(usage_basis) BETWEEN 1 AND 64),
    measurement_count INTEGER NOT NULL CHECK (measurement_count >= 0),
    held_measurement_count INTEGER NOT NULL CHECK (held_measurement_count >= 0),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    input_reported_count INTEGER NOT NULL CHECK (input_reported_count >= 0),
    input_missing_count INTEGER NOT NULL CHECK (input_missing_count >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    output_reported_count INTEGER NOT NULL CHECK (output_reported_count >= 0),
    output_missing_count INTEGER NOT NULL CHECK (output_missing_count >= 0),
    cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    cache_reported_count INTEGER NOT NULL CHECK (cache_reported_count >= 0),
    cache_missing_count INTEGER NOT NULL CHECK (cache_missing_count >= 0),
    cache_creation_input_tokens INTEGER CHECK (cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0),
    cache_creation_reported_count INTEGER NOT NULL CHECK (cache_creation_reported_count >= 0),
    cache_creation_missing_count INTEGER NOT NULL CHECK (cache_creation_missing_count >= 0),
    cache_read_input_tokens INTEGER CHECK (cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0),
    cache_read_reported_count INTEGER NOT NULL CHECK (cache_read_reported_count >= 0),
    cache_read_missing_count INTEGER NOT NULL CHECK (cache_read_missing_count >= 0),
    reasoning_output_tokens INTEGER CHECK (reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0),
    reasoning_reported_count INTEGER NOT NULL CHECK (reasoning_reported_count >= 0),
    reasoning_missing_count INTEGER NOT NULL CHECK (reasoning_missing_count >= 0),
    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
    total_reported_count INTEGER NOT NULL CHECK (total_reported_count >= 0),
    total_missing_count INTEGER NOT NULL CHECK (total_missing_count >= 0),
    estimated_cost_microusd INTEGER CHECK (estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0),
    cost_completeness TEXT NOT NULL CHECK (cost_completeness IN ('complete', 'partial', 'unavailable')),
    built_through_sequence INTEGER NOT NULL CHECK (built_through_sequence >= 0)
) STRICT;

CREATE UNIQUE INDEX uq_rm_usage_day_dimensions
    ON rm_usage_day(day, client, ifnull(provider, ''), ifnull(model, ''), usage_basis);
CREATE INDEX idx_rm_usage_day_range
    ON rm_usage_day(day DESC, rm_usage_day_id DESC);
CREATE INDEX idx_rm_usage_client_range
    ON rm_usage_day(client, day DESC, rm_usage_day_id DESC);
CREATE INDEX idx_rm_usage_model_range
    ON rm_usage_day(model, day DESC, rm_usage_day_id DESC);
