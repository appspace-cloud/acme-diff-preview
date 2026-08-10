"""v2.6.4 coverage pass 1 — pure helper branches.

Deterministic, zero-infrastructure unit tests closing the easy-to-reach
error and short-circuit branches in the small helper functions: retry-after
parsing, DIFF_REPOS / app-git-repo parsing skips, manifest parsing skips,
section-kind fallback, replicas-zeroed value guard, resource-diff no-op,
required-error fallback, comment truncation short-circuit, AI-summary /
error-detail empty short-circuits, and the precomputed-facts overflow note.
These are the arms the happy-path tests walk past without triggering.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402


# ── _parse_diff_repos: entry with empty slug (":scope" only) is skipped ───

def test_parse_diff_repos_skips_entry_with_empty_slug():
    # "  :gcp/" has a scope but no slug -> that entry is dropped (L140),
    # the valid one survives.
    repos = m._parse_diff_repos("acme-config-dev; :gcp/ ;acme-config-stage")
    assert set(repos) == {"acme-config-dev", "acme-config-stage"}


# ── _parse_retry_after: RFC-1123 date without tzinfo, and unparseable ─────

def test_parse_retry_after_http_date_is_seconds_from_now():
    # A naive (no-tz) far-future date exercises the parsedate path AND the
    # naive->UTC branch (L953): parsedate_to_datetime returns a tz-naive
    # datetime, which the code stamps as UTC before the delta. Result is a
    # positive integer of seconds.
    secs = m._parse_retry_after("21 Oct 2099 07:28:00")
    assert isinstance(secs, int) and secs > 0


def test_parse_retry_after_tz_aware_http_date_also_works():
    # The GMT form parses tz-aware, skipping L953 but covering the delta path.
    secs = m._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert isinstance(secs, int) and secs > 0


def test_parse_retry_after_past_date_clamps_to_zero():
    assert m._parse_retry_after("Wed, 21 Oct 1999 07:28:00 GMT") == 0


def test_parse_retry_after_unparseable_returns_none():
    # Garbage raises ValueError inside parsedate_to_datetime on modern
    # Python -> caught by the except, returns None.
    assert m._parse_retry_after("not-a-date-at-all") is None


# ── _extract_app_git_repo: source with empty repoURL is skipped ──────────

def test_extract_app_git_repo_skips_source_with_blank_url():
    # First non-chart source has an empty repoURL (L1309 continue); the
    # second provides the real slug.
    app = {"spec": {"sources": [
        {"repoURL": "   "},
        {"repoURL": "git@bitbucket.org:appspace-cloud/acme-config-dev.git"},
    ]}}
    assert m._extract_app_git_repo(app) == "acme-config-dev"


# ── _section_kind: malformed header falls back to "" ─────────────────────

def test_section_kind_extracts_last_segment():
    assert m._section_kind("/external-secrets.io/ExternalSecret name") == \
        "ExternalSecret"


def test_section_kind_on_non_string_returns_empty():
    # .rsplit on a non-str raises inside the try -> "" (L3181-3182).
    assert m._section_kind(None) == ""


# ── _detect_replicas_zeroed: judged on the applied state ────────────────

def test_detect_replicas_zeroed_ignores_unparseable_old_value():
    # An unreadable OLD value no longer suppresses the finding (COPS-2631).
    # Detection reads the "+" side only, because that is the state being
    # applied: whatever it was before, this workload ends up at 0 replicas
    # and a reviewer needs to be told. The previous expectation here encoded
    # the pairing requirement that made an environment-wide shutdown
    # invisible on acme-config-dev PR #7063. Header shape: _section_kind
    # takes the segment after the LAST "/", so the kind must sit there.
    kind = next(iter(m._WORKLOAD_KINDS))
    header = f"/apps/ns/{kind} appname"
    assert m._section_kind(header) == kind          # guard the header shape
    body = "-  replicas: notanint\n+  replicas: 0\n"
    assert m._detect_replicas_zeroed([(header, body)]) == [header]


def test_detect_replicas_zeroed_ignores_unparseable_new_value():
    kind = next(iter(m._WORKLOAD_KINDS))
    header = f"/apps/ns/{kind} appname"
    body = "-  replicas: 3\n+  replicas: notanint\n"
    assert m._detect_replicas_zeroed([(header, body)]) == []


def test_detect_replicas_zeroed_flags_real_transition():
    kind = next(iter(m._WORKLOAD_KINDS))
    header = f"/apps/ns/{kind} appname"
    body = "-  replicas: 3\n+  replicas: 0\n"
    assert m._detect_replicas_zeroed([(header, body)]) == [header]


# ── _explain_required_error: generic error (no template match) fallback ──

def test_explain_required_error_generic_fallback():
    out = m._explain_required_error("some unrelated helm failure\nsecond line")
    assert len(out) == 1
    assert out[0].startswith("> some unrelated helm failure")


def test_explain_required_error_empty_input():
    out = m._explain_required_error("")
    assert out == ["> no error output"]


# ── _truncate_comment: body under the limit is returned unchanged ────────

def test_truncate_comment_under_limit_is_identity():
    body = "small body\n---\n**Status:** ok\n"
    assert m._truncate_comment(body) == body


# ── _sanitize_ai_summary / _redact_error_detail: empty short-circuits ────

def test_sanitize_ai_summary_empty_returns_input():
    assert m._sanitize_ai_summary("") == ""
    assert m._sanitize_ai_summary(None) is None


def test_redact_error_detail_empty_returns_input():
    assert m._redact_error_detail("") == ""
    assert m._redact_error_detail(None) is None


# ── _precomputed_facts_note: >30 deletions emits the "+N more" line ──────

class _FakeRes:
    def __init__(self, deleted=None, zeroed=None):
        self.deleted_resources = deleted or []
        self.replicas_zeroed = zeroed or []


def test_precomputed_facts_note_overflow_more_line():
    # 35 deleted resources under one app -> first 30 listed, then the
    # "(+5 more)" summary line (L4990).
    dels = [f"/apps/Deployment ns/app{i}" for i in range(35)]
    note = m._precomputed_facts_note({"my-app": _FakeRes(deleted=dels)})
    assert "(+5 more)" in note
    assert "Resources DELETED entirely:" in note


def test_precomputed_facts_note_empty_when_nothing():
    assert m._precomputed_facts_note({"a": _FakeRes()}) == ""
