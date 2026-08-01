"""Value-pattern secret redaction: cut the secret, keep the record.

The original implementation replaced the WHOLE string on any pattern hit and
used unanchored patterns, so ordinary ledger data was destroyed: "task-",
"disk-" and "risk-" all contain "sk-", and the phrase "Bearer token" matched
the Authorization pattern. Worse than the loss, every destroyed section_id
collapsed onto the same literal placeholder, so unrelated jobs merged into one
phantom section. These tests pin both halves: real secrets still go, and the
false positives that caused the incident never touch a value again.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.event_log import RAW_EVENT_LOG_FILENAME, RawEventLog
from agentacct.service import (
    RESERVED_VALUE_REDACTION_KEYS,
    SentinelService,
)
from agentacct.usage_truth import (
    is_local_session_observation_event,
    mark_trusted_local_session_observation_event,
)


# Built by concatenation so the fixtures are unmistakably synthetic while
# keeping the exact shape the patterns must still recognize.
FAKE_API_KEY = "sk-" + "fakeonly" + "0" * 32 + "AbCd"
FAKE_OPENROUTER_KEY = "sk-or-v1-" + "fakeonly" + "0" * 40 + "EfGh"
FAKE_BEARER_HEADER = "Bearer " + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl"
# Credential alphabets that a fixed [A-Za-z0-9._~+/=-] charset cannot hold:
# percent-encoding and a basic-auth style colon pair.
FAKE_BEARER_OPAQUE = "Bearer " + "fake%3Auser:pass%2F" + "0" * 12
FAKE_BEARER_BASE64 = "Bearer " + "ZmFrZS1vbmx5" + "0" * 20 + "Kw=="

# The prose that the shipped discriminator destroyed: hyphenated engineering
# compounds, which are credential-shaped only if `-` counts as a credential
# character.
BEARER_PROSE = (
    "switch to Bearer token-based-authentication soon; "
    "Bearer tokens-are-rotated-nightly by the job, and "
    "we should use a Bearer token here."
)

# The negative half of the calibration corpus. Every phrase here must come back
# out of record_event byte for byte. Grouped by which shipped rule ate it.
LEDGER_PROSE_CORPUS = (
    # round 1: any non-delimiter run after "Bearer"
    "Bearer token",
    "use a Bearer token here",
    # round 2: narrowed charset requiring a digit or a separator
    "Bearer token-based-authentication",
    "Bearer tokens-are-rotated-nightly",
    # round 3: requiring a digit -- the auth vocabulary is full of them
    "Bearer oauth2-client-credentials migration",
    "debug Bearer sha256-hmac-signature mismatch",
    "Bearer x509-certificate-validation-chain rework",
    "Bearer rfc7519-jwt-claims-validation is in scope",
    "Bearer base64-url-encoded-payload decoding bug",
    "Bearer http2-connection-reuse regression",
    "Bearer sha512-digest-comparison helper",
    # round 3 also newly mangled these two
    "run with Bearer $CI_API_TOKEN_V2 in the header",
    "see Bearer https://auth.example.com/v2/token for the endpoint",
    # separators that a word compound reaches, so a bare separator test fails
    "the Bearer header/credential split needs a doc note",
    "Bearer token.rotation.policy documented",
    "Bearer refresh_token_rotation is enabled",
    "Bearer sso/saml2-assertion handoff",
    "Bearer client.credentials.grant2 flow",
    "Bearer jwt.decode.verify_signature=False was the bug",
    "Bearer scope=read:write granted to the app",
    "Bearer oauth2/openid-connect/discovery migration notes",
    # documentation placeholders, which are angle-bracketed and not secrets
    "Bearer <token>",
    "Bearer <your-token-here>",
    "Authorization: Bearer <your-access-token>",
    "Authorization: Bearer token is required for all endpoints",
    "the Authorization: Bearer header must be present on every request",
    # shapes the real ledger is full of: section ids, run ids, branches, paths,
    # snake_case identifiers, timestamps and Chinese narrative
    "Bearer aethermoor-progress-audit-20260713 section id",
    "Bearer eden-parent-plan-next-task-20260722 handoff",
    "Bearer rollout-2026-07 window",
    "Bearer phase-4-2-work-task-sessions rework",
    "Bearer release-v1-2-3-candidate build",
    "Bearer token expired at 2026-07-30T12:00:00Z",
    "Bearer auth is configured in src/agentacct/service.py already",
    "Bearer 认证令牌已经在昨天轮换完成，无需再次处理",
    "Bearer token 的轮换策略：每晚一次",
    "evidence_refreshable_usage_failed on claude/canonical-migration-4-work-task-sessions",
    "silly-hellman-ccc6a2 and momentum-audit-8292ca490713ed37d8edae41 both replayed",
    ".agent-sentinel/state/runs/run_20260717_023239_343b052e/report.md was regenerated",
    # the "sk-" incident values
    "task-2026-07-30-review",
    "disk-usage-report-2026",
    "risk-register-update-v2",
    "brisk-refactor-of-the-store",
    "the sk- prefix is what we match",
    "src/agentacct/work-task-sessions-4801fa",
)

# The positive half: one entry per credential shape the rule has to cut. All
# values are obviously fake and built by concatenation.
CREDENTIAL_CORPUS = (
    ("jwt_header_line", f"Authorization: {FAKE_BEARER_HEADER}"),
    (
        "curl_header",
        'curl -H "Authorization: ' + FAKE_BEARER_HEADER + '" https://api.example.com/v1/ping',
    ),
    ("proxy_header_line", f"Proxy-Authorization: {FAKE_BEARER_HEADER}"),
    ("authorization_equals", f"Authorization={FAKE_BEARER_BASE64}"),
    ("opaque_percent_colon", FAKE_BEARER_OPAQUE),
    ("base64_padded", FAKE_BEARER_BASE64),
    ("hex_digest", "Bearer " + "0f1e2d3c4b5a6978" + "8796a5b4c3d2e1f0"),
    ("uuid_token", "Bearer " + "3f9a1c72-5e4b-4c8d-9f21-7ab6d0e4c531"),
    ("long_digit_run", "Bearer " + "local-redaction-placeholder-1234567890"),
    (
        "opaque_alphabetic_in_header",
        "Authorization: Bearer " + "RkFLRXNlc3Npb250b2tlbnZhbHVl",
    ),
    ("midsentence_token", f"rotated the {FAKE_BEARER_HEADER}"),
    ("api_key", f"OPENAI_API_KEY={FAKE_API_KEY}"),
    ("openrouter_key", f"OPENROUTER_API_KEY={FAKE_OPENROUTER_KEY}"),
)


# Prefixed opaque provider tokens. Their own shape -- a short letter prefix, a
# separator, then an alphanumeric body -- reads as a word compound, so the prose
# refusal used to swallow them even though these are the most commonly leaked
# credentials in the world.
#
# Every body here is PURELY ALPHABETIC (uppercase for AWS), which makes each row
# independently proving: no long digit run, no >=32-character alphanumeric run,
# no UUID and no `. / + = % : @ | # &` separator, so none of them can reach the
# generic credential-shape branch, and the word-compound refusal covers all of
# them. Delete the provider family and all 15 rows fail. The earlier fixtures
# embedded "0" * 24, so 9 of the 12 passed through the >=10-digit branch and
# pinned nothing about their own prefix.
_FAKE_BODY_24 = "FakeOnly" + "AbCdEfGh" + "IjKlMnOp"
_FAKE_BODY_20 = "FakeOnly" + "AbCdEfGh" + "IjKl"
_FAKE_BODY_16_UPPER = "FAKEONLY" + "FAKEONLY"
FAKE_PROVIDER_TOKENS = (
    ("github_personal", "ghp_" + _FAKE_BODY_24),
    ("github_oauth", "gho_" + _FAKE_BODY_24),
    ("github_user_to_server", "ghu_" + _FAKE_BODY_24),
    ("github_server_to_server", "ghs_" + _FAKE_BODY_24),
    ("github_refresh", "ghr_" + _FAKE_BODY_24),
    ("github_fine_grained", "github_pat_" + _FAKE_BODY_24),
    ("gitlab_personal", "glpat-" + _FAKE_BODY_20),
    ("aws_access_key", "AKIA" + _FAKE_BODY_16_UPPER),
    ("aws_session_key", "ASIA" + _FAKE_BODY_16_UPPER),
    ("slack_bot", "xoxb-" + _FAKE_BODY_20),
    ("slack_user", "xoxp-" + _FAKE_BODY_20),
    ("google_api", "AIza" + _FAKE_BODY_24),
    ("huggingface", "hf_" + _FAKE_BODY_20),
    ("replicate", "r8_" + _FAKE_BODY_20),
    ("vendor_opaque", "tok_" + _FAKE_BODY_20),
)

# Prose whose compound carries a BARE NUMERIC piece. RFC 6750 is the Bearer
# Token specification itself, so the first line here is about the likeliest
# sentence in an engineering ledger to follow the word "Bearer"; the rest are
# shapes taken from the maintainer's own store.
BEARER_NUMERIC_PIECE_PROSE = (
    "Bearer rfc6750/section-2.1 says the header is required",
    "Bearer phase-4.2-work-task-sessions rework",
    "Bearer release/v1.2.3-candidate build",
    "Bearer canonical-migration-4.3 notes",
    "Bearer claude-4.5-sonnet-thinking was the model",
    "Bearer activation.py:49 is where the check lives",
)

# Runs that are word-shaped but sit inside a real Authorization header, where
# prose does not occur -- nobody writes an HTTP header in a sentence.
BEARER_HEADER_WORD_SHAPED = (
    ("hyphen_joined", "Authorization: Bearer ", "abcdefgh-ijklmnop-qrstuvwx"),
    ("dot_joined", "Authorization: Bearer ", "abcdefgh.ijklmnop.qrstuvwx"),
    ("proxy_hyphen_joined", "Proxy-Authorization: Bearer ", "abcdefgh-ijklmnop-qrstuvwx"),
    ("equals_form", "Authorization=Bearer ", "abcdefgh-ijklmnop-qrstuvwx"),
)

# Family 2 used to carry a >=16-character floor copied from family 3, so a SHORT
# credential inside a real header was kept while main cut it. The floor is now 8,
# and both sides of that boundary are pinned because both are load-bearing: 8..15
# is the window this closed, and 1..7 stays open on purpose so that the ledger's
# own sentences ABOUT the header ("Authorization: Bearer token is required...",
# run "token", 5 characters) survive. LEDGER_PROSE_CORPUS is where those live.
_SHORT_HEADER_MIXED = "a1b2c3d4e5f6g7h"
HEADER_CREDENTIALS_AT_OR_ABOVE_FLOOR = tuple(
    _SHORT_HEADER_MIXED[:length] for length in range(8, 16)
) + tuple("1" * length for length in range(8, 16))
HEADER_RUNS_BELOW_FLOOR = tuple(_SHORT_HEADER_MIXED[:length] for length in range(1, 8))

# Vendor prefixes added after the reviewer found family 1's own promise -- "a
# token pasted into a log or env line carries its own prefix" -- was false for
# them: BOTH main and this module kept every row below in an env line, a log line
# and a URL query. Bodies are purely alphabetic (plus the fixed PyPI macaroon
# opener) so each row is proven by its prefix alone, not by a digit run or a
# >=32-character alphanumeric run.
_FAKE_BODY_32 = "FakeOnly" + "AbCdEfGh" + "IjKlMnOp" + "QrStUvWx"
FAKE_LATE_PROVIDER_TOKENS = (
    ("stripe_live", "sk_live_" + _FAKE_BODY_24),
    ("stripe_test", "sk_test_" + _FAKE_BODY_24),
    ("npm_access", "npm_" + _FAKE_BODY_32),
    ("pypi_upload", "pypi-AgE" + _FAKE_BODY_32),
    ("shopify_admin", "shpat_" + _FAKE_BODY_24),
    ("square_access", "sq0atp-" + _FAKE_BODY_20),
    ("linear_api", "lin_api_" + _FAKE_BODY_32),
    ("digitalocean_pat", "dop_v1_" + _FAKE_BODY_32 + _FAKE_BODY_24),
    ("stripe_restricted_live", "rk_live_" + _FAKE_BODY_24),
    ("stripe_restricted_test", "rk_test_" + _FAKE_BODY_24),
)

# The `pypi-` prefix alone is NOT enough evidence: this is a real value from the
# maintainer's store, and `pypi-[A-Za-z0-9_-]{16,}` would destroy it. Anchoring
# on the macaroon body (`AgE`) is what keeps it.
PYPI_SHAPED_LEDGER_PROSE = "pypi-publish-and-install-smoke"

# KNOWN GAPS (b): a credential inside a URL is kept after a BARE "Bearer",
# because the URL refusal that protects the quoted URLs this ledger is full of
# fires first. A header still catches it.
URL_EMBEDDED_CREDENTIAL = "postgres://user:s3cr3tpassw0rdvalue@db.example.com:5432/app"

# KNOWN GAPS (c): the one gap a header does NOT close. `;` is not a run
# character, so the match ends at the first semicolon and the AccountKey after it
# survives in every serialization.
CONNECTION_STRING_SECRET = _FAKE_BODY_32
CONNECTION_STRING = (
    "DefaultEndpointsProtocol=https;AccountName=acctname;AccountKey="
    + CONNECTION_STRING_SECRET
)


def _session_observation(*, title: str) -> dict:
    return {
        "source": "codex-local-session-observation-import",
        "event_type": "session_observed",
        "run_id": "client_codex_redaction_marker",
        "metadata": {
            "client": "codex",
            "client_session_id": "redaction-marker-session",
            "client_session_kind": "root",
            "source_namespace_fingerprint": "sha256:" + "b" * 64,
            "project_dir": "/tmp/observed-project",
            "started_at": 100.0,
            "updated_at": 200.0,
            "source_parse_complete": True,
            "client_session_title": title,
            "client_session_title_source": "explicit_client_title_field",
            "client_session_title_sanitized": True,
            "title_redacted": False,
        },
    }


def _stored_text(store: Path) -> str:
    lines = RawEventLog(store / RAW_EVENT_LOG_FILENAME).read_lines()
    return "".join(line + "\n" for line in lines)


def _stored_events(store: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in _stored_text(store).splitlines()
        if line.strip()
    ]


def test_real_shaped_secrets_are_still_removed_from_the_stored_event(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "openai": FAKE_API_KEY,
                "openrouter": FAKE_OPENROUTER_KEY,
                "header": FAKE_BEARER_HEADER,
            },
        }
    )

    assert recorded["metadata"]["openai"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["openrouter"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["header"] == "[REDACTED_SECRET]"
    stored = _stored_text(store)
    for secret in (FAKE_API_KEY, FAKE_OPENROUTER_KEY, FAKE_BEARER_HEADER):
        assert secret not in stored
    # The openrouter shape is a prefix-superset of the plain key shape; it must
    # report as the specific class, not the generic one.
    classes = {row["pattern_class"] for row in recorded["metadata"]["value_redaction_fields"]}
    assert classes == {"api_key", "openrouter_api_key", "bearer_token"}


def test_surrounding_prose_survives_a_redacted_secret(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "summary": (
                    f"Rotated the provider key to {FAKE_API_KEY} after the outage. "
                    "The dashboard came back clean."
                )
            },
        }
    )

    assert recorded["metadata"]["summary"] == (
        "Rotated the provider key to [REDACTED_SECRET] after the outage. "
        "The dashboard came back clean."
    )
    assert FAKE_API_KEY not in _stored_text(store)


def test_incident_false_positives_leave_every_value_untouched(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    project_dir = "/Users/x/.claude/worktrees/canonical-migration-4-work-task-sessions-4801fa"

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "section_id": "work-task-sessions-4801fa",
            "project_dir": project_dir,
            "branch": "feat/task-sessions-4801fa",
            "metadata": {
                "summary": (
                    "disk-usage climbed while risk-assessment-2026 was open; "
                    "we should use a Bearer token here."
                ),
                "idempotency_key": "task-sessions-4801fa-attempt-1",
            },
        }
    )

    assert recorded["section_id"] == "work-task-sessions-4801fa"
    assert recorded["project_dir"] == project_dir
    assert recorded["branch"] == "feat/task-sessions-4801fa"
    assert recorded["metadata"]["summary"] == (
        "disk-usage climbed while risk-assessment-2026 was open; "
        "we should use a Bearer token here."
    )
    assert recorded["metadata"]["idempotency_key"] == "task-sessions-4801fa-attempt-1"
    assert "[REDACTED" not in _stored_text(store)


def test_two_task_shaped_ids_stay_distinct_in_the_ledger(tmp_path: Path) -> None:
    """The merge bug: both ids used to collapse onto the literal placeholder."""

    store = tmp_path / "state"
    service = SentinelService(store)

    first = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "section_id": "canonical-migration-4-work-task-sessions-4801fa",
            "metadata": {"idempotency_key": "work-task-sessions-4801fa-checkpoint"},
        }
    )
    second = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "section_id": "dashboard-rewrite-2-work-task-findings-9c31bb",
            "metadata": {"idempotency_key": "work-task-findings-9c31bb-checkpoint"},
        }
    )

    assert first["section_id"] != second["section_id"]
    assert first["metadata"]["idempotency_key"] != second["metadata"]["idempotency_key"]
    assert first["event_id"] != second["event_id"]
    stored = _stored_events(store)
    assert len(stored) == 2
    assert {row["section_id"] for row in stored} == {
        "canonical-migration-4-work-task-sessions-4801fa",
        "dashboard-rewrite-2-work-task-findings-9c31bb",
    }


def test_marker_names_the_redacted_fields_and_pattern_classes(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "branch": f"feat/{FAKE_API_KEY}",
            "metadata": {
                "summary": f"header was {FAKE_BEARER_HEADER}",
                "items": [{"note": FAKE_OPENROUTER_KEY}],
            },
        }
    )

    assert recorded["metadata"]["value_redaction_applied"] is True
    assert recorded["metadata"]["value_redaction_fields"] == [
        {"field": "branch", "pattern_class": "api_key"},
        {"field": "metadata.items.0.note", "pattern_class": "openrouter_api_key"},
        {"field": "metadata.summary", "pattern_class": "bearer_token"},
    ]


def test_marker_is_absent_when_nothing_was_redacted(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "project_dir": "/repos/brisk-task-runner",
            "metadata": {"summary": "disk-usage report for risk-assessment-2026"},
        }
    )

    assert "value_redaction_applied" not in recorded["metadata"]
    assert "value_redaction_fields" not in recorded["metadata"]


def test_a_forged_redaction_marker_is_stripped_from_caller_metadata(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "summary": "nothing sensitive here",
                "value_redaction_applied": True,
                "value_redaction_fields": [{"field": "metadata.summary", "pattern_class": "api_key"}],
            },
        }
    )

    assert "value_redaction_applied" not in recorded["metadata"]
    assert "value_redaction_fields" not in recorded["metadata"]
    assert recorded["metadata"]["reserved_value_redaction_provenance_stripped"] is True


def test_sensitive_key_names_still_lose_the_whole_value(tmp_path: Path) -> None:
    """Key-name decisions are unchanged: only the value-pattern path narrowed."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "api_key": "some prose and " + FAKE_API_KEY + " and more prose",
                "safe": "kept",
            },
        }
    )

    assert recorded["metadata"]["api_key"] == "[REDACTED]"
    assert recorded["metadata"]["safe"] == "kept"
    # Whole-value key redaction is self-evident from the key name, so it does
    # not claim a value-pattern redaction.
    assert "value_redaction_applied" not in recorded["metadata"]
    assert FAKE_API_KEY not in _stored_text(store)


def test_a_bearer_credential_outside_the_token_charset_is_cut_whole(tmp_path: Path) -> None:
    """A fixed credential alphabet leaked the whole key on the first `%`.

    "summary" is not a sensitive key NAME, so the value pattern is the only
    defence here: with the charset stopping at `%`, the run after "Bearer "
    was just "abc", fell under the length floor, and nothing matched at all.
    """

    store = tmp_path / "state"
    service = SentinelService(store)
    credential = "abc%2Fdefghijklmnopqrstuvwx"

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {"summary": f"curl failed: Authorization: Bearer {credential}"},
        }
    )

    assert recorded["metadata"]["summary"] == "curl failed: Authorization: [REDACTED_SECRET]"
    assert credential not in _stored_text(store)


def test_hyphenated_prose_after_bearer_is_left_alone(tmp_path: Path) -> None:
    """Counting `-` as credential-shaped shredded ordinary engineering prose."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "branch": "feat/bearer-token-based-authentication",
            "metadata": {"summary": BEARER_PROSE},
        }
    )

    assert recorded["metadata"]["summary"] == BEARER_PROSE
    assert recorded["branch"] == "feat/bearer-token-based-authentication"
    assert "[REDACTED" not in _stored_text(store)


def test_every_credential_shaped_bearer_value_is_still_cut(tmp_path: Path) -> None:
    """The other half of the prose rule: real credential shapes still go."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "jwt": FAKE_BEARER_HEADER,
                "opaque": FAKE_BEARER_OPAQUE,
                "base64": FAKE_BEARER_BASE64,
                "header_line": f"Authorization: {FAKE_BEARER_HEADER}",
            },
        }
    )

    assert recorded["metadata"]["jwt"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["opaque"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["base64"] == "[REDACTED_SECRET]"
    assert recorded["metadata"]["header_line"] == "Authorization: [REDACTED_SECRET]"
    stored = _stored_text(store)
    for secret in (FAKE_BEARER_HEADER, FAKE_BEARER_OPAQUE, FAKE_BEARER_BASE64):
        assert secret not in stored


def test_the_calibration_corpus_survives_the_bearer_rule_byte_for_byte(
    tmp_path: Path,
) -> None:
    """Prose the rule must never touch, sampled from a real 26773-value ledger.

    Three shipped bearer rules in a row destroyed ordinary engineering prose,
    each time in the same direction, because each was guessed rather than
    measured. These literals are the calibration set: the phrases every earlier
    rule was caught on, plus the shapes the maintainer's own ledger is actually
    full of (date-suffixed section ids, hashed run ids, snake_case identifiers,
    paths, Chinese). Copied in as literals on purpose -- the test must not read
    anybody's store.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    for index, phrase in enumerate(LEDGER_PROSE_CORPUS):
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"summary": phrase},
            }
        )
        assert recorded["metadata"]["summary"] == phrase, (index, phrase)
        assert "value_redaction_applied" not in recorded["metadata"], phrase

    assert "[REDACTED" not in _stored_text(store)


def test_every_calibration_credential_shape_is_cut_out_of_the_value(
    tmp_path: Path,
) -> None:
    """The other side of the same calibration: each leak shape still goes."""

    store = tmp_path / "state"
    service = SentinelService(store)

    for label, phrase in CREDENTIAL_CORPUS:
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"summary": phrase},
            }
        )
        summary = recorded["metadata"]["summary"]
        assert "[REDACTED_SECRET]" in summary, (label, summary)
        assert recorded["metadata"]["value_redaction_applied"] is True, label

    stored = _stored_text(store)
    for label, phrase in CREDENTIAL_CORPUS:
        secret = phrase.split()[-1] if label == "midsentence_token" else phrase
        assert secret not in stored, label


def test_a_shell_variable_or_url_after_bearer_is_not_a_credential(
    tmp_path: Path,
) -> None:
    """Two shapes the digit rule newly mangled; both are references, not secrets.

    `$CI_API_TOKEN_V2` is the name of a secret, not the secret, and the endpoint
    URL is public. Cutting either would lose ledger data and protect nothing.
    """

    store = tmp_path / "state"
    service = SentinelService(store)
    shell = "run with Bearer $CI_API_TOKEN_V2 in the header"
    url = "see Bearer https://auth.example.com/v2/token for the endpoint"

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {"shell": shell, "url": url},
        }
    )

    assert recorded["metadata"]["shell"] == shell
    assert recorded["metadata"]["url"] == url


def test_redacting_an_already_redacted_value_changes_nothing(tmp_path: Path) -> None:
    """The widened run must not be able to eat its own placeholder."""

    store = tmp_path / "state"
    service = SentinelService(store)
    once = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {"summary": f"header was {FAKE_BEARER_OPAQUE}"},
        }
    )

    twice = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {"summary": once["metadata"]["summary"]},
        }
    )

    assert twice["metadata"]["summary"] == once["metadata"]["summary"]
    assert "value_redaction_applied" not in twice["metadata"]


def test_the_marker_never_names_a_field_the_server_reassigns(tmp_path: Path) -> None:
    """event_id is overwritten, so a marker row naming it points at nothing."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "event_id": "caller-" + FAKE_API_KEY,
            "metadata": {"summary": f"rotated {FAKE_API_KEY} today"},
        }
    )

    assert recorded["event_id"].startswith("evt_")
    assert "[REDACTED_SECRET]" not in recorded["event_id"]
    assert recorded["metadata"]["value_redaction_fields"] == [
        {"field": "metadata.summary", "pattern_class": "api_key"}
    ]
    assert FAKE_API_KEY not in _stored_text(store)


def test_a_reassigned_field_alone_leaves_no_marker_at_all(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "event_id": "caller-" + FAKE_API_KEY,
            "metadata": {"summary": "nothing sensitive here"},
        }
    )

    assert recorded["event_id"].startswith("evt_")
    assert "value_redaction_applied" not in recorded["metadata"]
    assert "value_redaction_fields" not in recorded["metadata"]


def test_a_forged_top_level_redaction_marker_is_stripped(tmp_path: Path) -> None:
    """The stamp lands top-level for off-contract metadata, so a caller can
    plant it there too; that home is cleared like the metadata one."""

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "value_redaction_applied": True,
            "value_redaction_fields": [
                {"field": "metadata.summary", "pattern_class": "api_key"}
            ],
            "metadata": {"summary": "nothing sensitive here"},
        }
    )

    assert "value_redaction_applied" not in recorded
    assert "value_redaction_fields" not in recorded
    assert recorded["metadata"]["reserved_value_redaction_provenance_stripped"] is True


def test_a_session_observation_says_that_its_title_was_cut(tmp_path: Path) -> None:
    """A trusted observation used to lose the marker to the allowlist rebuild.

    The title was stored correctly redacted and nothing recorded that data had
    been removed, which is the silent-cut failure the marker exists to prevent.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        _session_observation(title=f"rotated {FAKE_API_KEY} today"),
        trusted_session_observation_import=True,
    )

    assert recorded["metadata"]["client_session_title"] == "rotated [REDACTED_SECRET] today"
    assert recorded["metadata"]["value_redaction_applied"] is True
    assert recorded["metadata"]["value_redaction_fields"] == [
        {"field": "metadata.client_session_title", "pattern_class": "api_key"}
    ]
    # The marked row must still read back as a trusted observation: the lane
    # rejects any metadata key outside its allowlist.
    assert is_local_session_observation_event(recorded)
    assert FAKE_API_KEY not in _stored_text(store)


def test_a_repeated_observation_import_is_still_one_revision(tmp_path: Path) -> None:
    """The marker is part of the row, so it must be part of a stable digest."""

    store = tmp_path / "state"
    service = SentinelService(store)
    observation = _session_observation(title=f"rotated {FAKE_API_KEY} today")

    first = service.record_event(dict(observation), trusted_session_observation_import=True)
    second = service.record_event(dict(observation), trusted_session_observation_import=True)

    assert first["metadata"]["observation_revision"] == second["metadata"]["observation_revision"]
    assert len(_stored_events(store)) == 1


def test_a_prefixed_provider_token_after_a_bare_bearer_is_cut(tmp_path: Path) -> None:
    """A coverage regression the word-compound refusal introduced.

    `ghp_...`, `glpat-...` and `AKIA...` are a letter prefix joined to an
    alphanumeric body, which is exactly the shape the prose refusal exists to
    protect, so they stopped being redacted outside an Authorization header --
    while remaining the credentials most likely to actually leak.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    for label, token in FAKE_PROVIDER_TOKENS:
        phrase = f"the job logged Bearer {token} to stdout"
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"summary": phrase},
            }
        )
        assert recorded["metadata"]["summary"] == (
            "the job logged [REDACTED_SECRET] to stdout"
        ), label
        assert recorded["metadata"]["value_redaction_applied"] is True, label

    stored = _stored_text(store)
    for label, token in FAKE_PROVIDER_TOKENS:
        assert token not in stored, label


def test_a_prefixed_provider_token_is_cut_with_no_bearer_word_at_all(
    tmp_path: Path,
) -> None:
    """Family 1 is context-free: the prefix alone is the evidence.

    While the provider prefixes lived inside the `Bearer`-anchored pattern, a
    token pasted into an env line, a log line or a shell transcript -- which is
    how they actually leak -- was stored verbatim.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    for label, token in FAKE_PROVIDER_TOKENS:
        phrase = f"export PROVIDER_TOKEN={token} && ./deploy.sh"
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"summary": phrase},
            }
        )
        assert recorded["metadata"]["summary"] == (
            "export PROVIDER_TOKEN=[REDACTED_SECRET] && ./deploy.sh"
        ), label
        assert recorded["metadata"]["value_redaction_fields"] == [
            {"field": "metadata.summary", "pattern_class": "provider_token"}
        ], label

    stored = _stored_text(store)
    for label, token in FAKE_PROVIDER_TOKENS:
        assert token not in stored, label


def test_a_json_serialized_authorization_header_is_a_credential(
    tmp_path: Path,
) -> None:
    """The serialization this ledger stores most often, and it regressed once.

    `{"Authorization": "Bearer ..."}` puts a quote between the header name and
    the colon, which ended the header match, so the branch fell through to the
    prose rule and a hyphen-joined opaque token was kept verbatim.
    """

    store = tmp_path / "state"
    service = SentinelService(store)
    credential = "abcdefgh-ijklmnop-qrstuvwx"

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "json_body": '{"Authorization": "Bearer ' + credential + '"}',
                "single_quoted": "{'Proxy-Authorization': 'Bearer " + credential + "'}",
                "curl_arg": '-H "Authorization: Bearer ' + credential + '"',
            },
        }
    )

    assert recorded["metadata"]["json_body"] == '{"Authorization": "[REDACTED_SECRET]"}'
    assert recorded["metadata"]["single_quoted"] == (
        "{'Proxy-Authorization': '[REDACTED_SECRET]'}"
    )
    assert recorded["metadata"]["curl_arg"] == '-H "Authorization: [REDACTED_SECRET]"'
    assert credential not in _stored_text(store)


def test_the_separator_free_miss_window_is_where_known_gaps_says_it_is(
    tmp_path: Path,
) -> None:
    """Pin the KNOWN GAPS boundary so the comment cannot drift off the code.

    Outside a header the word-compound refusal reads a run as ONE word when it
    is at most 32 letters, optionally followed by at most 4 digits. Everything
    inside that window is kept; the first character past it is cut. The comment
    quotes these exact strings, so a change to the recogniser fails here.
    """

    store = tmp_path / "state"
    service = SentinelService(store)
    kept = ("a" * 16, "a" * 32, "a" * 32 + "2024")
    cut = ("a" * 33, "a" * 32 + "12345")

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                **{f"kept_{index}": f"Bearer {run}" for index, run in enumerate(kept)},
                **{f"cut_{index}": f"Bearer {run}" for index, run in enumerate(cut)},
            },
        }
    )

    for index, run in enumerate(kept):
        assert recorded["metadata"][f"kept_{index}"] == f"Bearer {run}", run
    for index, run in enumerate(cut):
        assert recorded["metadata"][f"cut_{index}"] == "[REDACTED_SECRET]", run


def test_a_compound_with_a_bare_number_in_it_is_still_prose(tmp_path: Path) -> None:
    """"Bearer rfc6750/section-2.1" is the Bearer spec, not a Bearer token."""

    store = tmp_path / "state"
    service = SentinelService(store)

    for phrase in BEARER_NUMERIC_PIECE_PROSE:
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"summary": phrase},
            }
        )
        assert recorded["metadata"]["summary"] == phrase, phrase
        assert "value_redaction_applied" not in recorded["metadata"], phrase

    assert "[REDACTED" not in _stored_text(store)


def test_a_word_shaped_run_inside_an_authorization_header_is_a_credential(
    tmp_path: Path,
) -> None:
    """The header branch is the backstop, so the prose refusals stop at it.

    Applying the word-compound refusal inside `Authorization:` left real
    hyphen- and dot-joined opaque tokens in the ledger verbatim, which is the
    one place the surrounding text proves the run is a credential.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    for label, header, credential in BEARER_HEADER_WORD_SHAPED:
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"summary": f"curl sent {header}{credential}"},
            }
        )
        expected_keep = header[: header.lower().index("bearer")]
        assert recorded["metadata"]["summary"] == (
            f"curl sent {expected_keep}[REDACTED_SECRET]"
        ), label
        assert credential not in _stored_text(store), label


def test_a_short_credential_inside_a_header_is_still_a_credential(
    tmp_path: Path,
) -> None:
    """Family 2's floor is 8, not the 16 it inherited from family 3.

    At 16 the header branch kept short credentials that main cut:
    `Authorization: Bearer a1b2c3d4e5f6g7h` (15) was stored verbatim while the
    same header at 16 was cut, and a pure-digit credential survived at every
    length 8..15. Those are the strings pinned here.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    for credential in HEADER_CREDENTIALS_AT_OR_ABOVE_FLOOR:
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {
                    "header": f"Authorization: Bearer {credential}",
                    "proxy": f"Proxy-Authorization: Bearer {credential}",
                    "json_body": '{"Authorization": "Bearer ' + credential + '"}',
                    "equals_form": f"Authorization=Bearer {credential}",
                },
            }
        )
        assert recorded["metadata"]["header"] == "Authorization: [REDACTED_SECRET]", credential
        assert recorded["metadata"]["proxy"] == (
            "Proxy-Authorization: [REDACTED_SECRET]"
        ), credential
        assert recorded["metadata"]["json_body"] == (
            '{"Authorization": "[REDACTED_SECRET]"}'
        ), credential
        assert recorded["metadata"]["equals_form"] == (
            "Authorization=[REDACTED_SECRET]"
        ), credential
        assert recorded["metadata"]["value_redaction_applied"] is True, credential


def test_the_header_floor_still_stops_below_eight_characters(tmp_path: Path) -> None:
    """The other side of the boundary, which is why the floor is not zero.

    The pinned prose corpus contains sentences ABOUT the header whose run after
    the word is "token" (5) and "header" (6). Removing the floor outright cuts
    those, so 1..7 stays open deliberately -- and the same runs after a BARE
    "Bearer", with no header name in front of them, stay open at every length.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    for run in HEADER_RUNS_BELOW_FLOOR:
        header = f"Authorization: Bearer {run}"
        bare = f"Bearer {run}"
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"header": header, "bare": bare},
            }
        )
        assert recorded["metadata"]["header"] == header, run
        assert recorded["metadata"]["bare"] == bare, run
        assert "value_redaction_applied" not in recorded["metadata"], run

    for credential in HEADER_CREDENTIALS_AT_OR_ABOVE_FLOOR:
        phrase = f"Bearer {credential}"
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {"summary": phrase},
            }
        )
        assert recorded["metadata"]["summary"] == phrase, credential
        assert "value_redaction_applied" not in recorded["metadata"], credential


def test_the_late_vendor_prefixes_are_cut_with_no_bearer_word_at_all(
    tmp_path: Path,
) -> None:
    """The leak paths family 1 claims to cover, for the vendors it had missed.

    Each of these was kept by BOTH main and the previous revision in an env
    export line, a `token:` log line and a URL query parameter, which made the
    family's own "carries its own prefix" promise false for them.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    for label, token in FAKE_LATE_PROVIDER_TOKENS:
        recorded = service.record_event(
            {
                "source": "test",
                "event_type": "note",
                "metadata": {
                    "env_line": f'export SERVICE_TOKEN="{token}"',
                    "log_line": f"token: {token}",
                    "url_query": f"https://api.example.com/v1/ping?access_token={token}",
                },
            }
        )
        assert recorded["metadata"]["env_line"] == (
            'export SERVICE_TOKEN="[REDACTED_SECRET]"'
        ), label
        assert recorded["metadata"]["log_line"] == "token: [REDACTED_SECRET]", label
        assert recorded["metadata"]["url_query"] == (
            "https://api.example.com/v1/ping?access_token=[REDACTED_SECRET]"
        ), label
        assert {"field": "metadata.env_line", "pattern_class": "provider_token"} in (
            recorded["metadata"]["value_redaction_fields"]
        ), label

    stored = _stored_text(store)
    for label, token in FAKE_LATE_PROVIDER_TOKENS:
        assert token not in stored, label


def test_the_pypi_prefix_is_anchored_on_the_macaroon_not_on_the_word(
    tmp_path: Path,
) -> None:
    """`pypi-` alone is a hyphenated English compound this ledger really writes.

    A bare `pypi-[A-Za-z0-9_-]{16,}` alternative would cut the real store value
    pinned here, so the alternative requires the `AgE` that opens every PyPI
    upload token's serialized macaroon.
    """

    store = tmp_path / "state"
    service = SentinelService(store)

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "workflow": f"ran {PYPI_SHAPED_LEDGER_PROSE} on the release tag",
                "lowercase_lookalike": "pypi-agentacct-publish-and-verify-step",
            },
        }
    )

    assert recorded["metadata"]["workflow"] == (
        f"ran {PYPI_SHAPED_LEDGER_PROSE} on the release tag"
    )
    assert recorded["metadata"]["lowercase_lookalike"] == (
        "pypi-agentacct-publish-and-verify-step"
    )
    assert "value_redaction_applied" not in recorded["metadata"]


def test_a_credential_in_a_url_is_kept_after_a_bare_bearer_and_cut_in_a_header(
    tmp_path: Path,
) -> None:
    """Pin KNOWN GAPS (b) on both sides, so neither half can drift.

    Family 3 refuses anything matching `scheme://` because ledger prose quotes
    URLs constantly, so a password inside one survives a bare "Bearer" that main
    would have cut. The header serializations are where it is still caught, and
    that half is what makes the gap acceptable.
    """

    store = tmp_path / "state"
    service = SentinelService(store)
    credential = URL_EMBEDDED_CREDENTIAL

    recorded = service.record_event(
        {
            "source": "test",
            "event_type": "note",
            "metadata": {
                "bare": f"Bearer {credential}",
                "header": f"Authorization: Bearer {credential}",
                "proxy": f"Proxy-Authorization: Bearer {credential}",
                "json_body": '{"Authorization": "Bearer ' + credential + '"}',
                "equals_form": f"Authorization=Bearer {credential}",
            },
        }
    )

    assert recorded["metadata"]["bare"] == f"Bearer {credential}"
    assert recorded["metadata"]["header"] == "Authorization: [REDACTED_SECRET]"
    assert recorded["metadata"]["proxy"] == "Proxy-Authorization: [REDACTED_SECRET]"
    assert recorded["metadata"]["json_body"] == '{"Authorization": "[REDACTED_SECRET]"}'
    assert recorded["metadata"]["equals_form"] == "Authorization=[REDACTED_SECRET]"


def test_a_connection_string_keeps_its_account_key_even_inside_a_header(
    tmp_path: Path,
) -> None:
    """Pin KNOWN GAPS (c), the one gap a header does NOT close.

    `;` is not a run character, so the match ends at the first semicolon and
    only "DefaultEndpointsProtocol=https" is replaced. The AccountKey after it
    survives in every serialization, including the header ones. main erased the
    whole value instead. This test exists so the comment cannot quietly claim
    the header covers this shape too: it does not, and the honest backstop is
    is_sensitive_metadata_key() on the field name.
    """

    store = tmp_path / "state"
    service = SentinelService(store)
    phrases = {
        "bare": f"Bearer {CONNECTION_STRING}",
        "header": f"Authorization: Bearer {CONNECTION_STRING}",
        "json_body": '{"Authorization": "Bearer ' + CONNECTION_STRING + '"}',
        "equals_form": f"Authorization=Bearer {CONNECTION_STRING}",
    }

    recorded = service.record_event(
        {"source": "test", "event_type": "note", "metadata": dict(phrases)}
    )

    assert recorded["metadata"]["bare"] == phrases["bare"]
    for field in ("header", "json_body", "equals_form"):
        assert CONNECTION_STRING_SECRET in recorded["metadata"][field], field
    assert CONNECTION_STRING_SECRET in _stored_text(store)


def test_the_observation_allowlist_keeps_server_authored_marker_keys() -> None:
    """Unit guard for the two lists that have to agree across modules."""

    marked = mark_trusted_local_session_observation_event(
        {
            **_session_observation(title="rotated [REDACTED_SECRET] today"),
            "metadata": {
                **_session_observation(title="rotated [REDACTED_SECRET] today")["metadata"],
                "value_redaction_applied": True,
                "value_redaction_fields": [
                    {"field": "metadata.client_session_title", "pattern_class": "api_key"}
                ],
            },
        }
    )

    for key in RESERVED_VALUE_REDACTION_KEYS:
        assert key in marked["metadata"]
    assert is_local_session_observation_event(marked)
