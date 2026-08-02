"""COPS-2575 - a render in flight must be abortable by a newer push.

Field report, acme-config-prod PR 3837, v2.17.1. Two force-pushes 8s apart.
The first render ran 190s to completion against a commit that was already
dead, published its diff into the shared PR comment, and only then did the
real commit get rendered. Total 6m17s, half of it wasted, and for ~10s the
PR showed a build status for one commit next to a comment describing
another.

The webhook already carries everything needed to know the tip moved:
pullrequest.id, pullrequest.source.commit.hash and repository.full_name.
do_POST read the body, HMAC-verified it, and then threw it away, deciding
purely on the X-Event-Key header.

Two invariants this file defends, in priority order:

1. THE WAKE PATH IS SACRED. This change is the first thing that ever parses
   the webhook body, so it is the first thing that could break the wake. A
   broken wake fails silently: the service just degrades to the 60s
   safety-net tick and everything feels sluggish until someone notices. Every
   hostile payload below must still return 200 and still wake the loop.
2. A superseded run publishes nothing: no comment, no build status, and
   crucially no _seen entry, so the PR is re-rendered rather than skipped.
"""
import hashlib
import hmac as hmac_mod
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402

SECRET = "s3cr3t-for-cops2575"
REPO = "acme-config-prod"
PR_ID = 3837
OLD_SHA = "76a9adc8f1e2"
NEW_SHA = "370a4122bbcd"


@pytest.fixture()
def health(monkeypatch):
    srv = m._start_health_server(0)
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _clean_supersede_state(monkeypatch):
    """Each test starts with empty hint state and a cleared wake.

    REPOS is patched to include acme-config-prod because this file replays
    the real PR 3837 scenario; the test default for DIFF_REPOS is
    acme-config-dev only, and an unrecognised slug correctly yields no hint.
    """
    monkeypatch.setitem(m.REPOS, REPO, {"scopes": []})
    with m._supersede_lock:
        m._pr_superseded.clear()
        m._pr_supersede_aborts.clear()
    m._wake.clear()
    yield
    with m._supersede_lock:
        m._pr_superseded.clear()
        m._pr_supersede_aborts.clear()
    m._wake.clear()


def _post(url, body: bytes, secret=SECRET, event="pullrequest:updated", extra=None):
    headers = {"X-Event-Key": event, "Content-Type": "application/json"}
    if secret:
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature"] = f"sha256={sig}"
    if extra:
        headers.update(extra)
    req = urllib.request.Request(f"{url}/diff-preview/webhook", data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _payload(pr_id=PR_ID, sha=NEW_SHA, full_name=f"appspace-cloud/{REPO}"):
    return json.dumps({
        "pullrequest": {"id": pr_id, "source": {"commit": {"hash": sha}}},
        "repository": {"full_name": full_name, "name": "Some Display Name"},
    }).encode()


# ── 1. the wake path, which must never break ─────────────────────────────

def test_signed_webhook_in_strict_mode_wakes_the_loop(health, monkeypatch):
    """The gap COPS-2575 found: no test proved a SIGNED webhook wakes the loop.

    Every pre-existing wake assertion ran with BB_WEBHOOK_SECRET patched to
    "", i.e. permissive mode, which is not what production runs. The only
    strict-mode test asserted the 401 rejection path.
    """
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", SECRET)
    code, _ = _post(health, _payload())
    assert code == 200
    assert m._wake.is_set(), "a signed pullrequest:* event must wake the diff loop"


HOSTILE = [
    ("valid", _payload()),
    ("extra_keys", json.dumps({
        "pullrequest": {"id": PR_ID, "source": {"commit": {"hash": NEW_SHA}},
                        "unknown": {"deeply": {"nested": [1, 2, 3]}}},
        "repository": {"full_name": f"appspace-cloud/{REPO}"},
        "actor": {"nope": True},
    }).encode()),
    ("empty_object", b"{}"),
    ("pullrequest_null", b'{"pullrequest": null}'),
    ("no_source", b'{"pullrequest": {"id": 1}}'),
    ("no_hash", b'{"pullrequest": {"id": 1, "source": {"commit": {}}}}'),
    ("hash_not_string", b'{"pullrequest": {"id": 1, "source": {"commit": {"hash": 42}}}}'),
    ("id_not_int", b'{"pullrequest": {"id": "abc", "source": {"commit": {"hash": "aa"}}}}'),
    ("unknown_repo", json.dumps({
        "pullrequest": {"id": PR_ID, "source": {"commit": {"hash": NEW_SHA}}},
        "repository": {"full_name": "someone-else/not-ours"},
    }).encode()),
    ("json_list", b'[1, 2, 3]'),
    ("json_string", b'"just a string"'),
    ("not_json", b"this is not json at all"),
    ("non_utf8", b"\xff\xfe\x00\x01 binary garbage"),
]


@pytest.mark.parametrize("label,body", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_hostile_payload_still_returns_200_and_still_wakes(health, monkeypatch, label, body):
    """The wake is non-negotiable. The hint is best-effort.

    This is the test that stops a future refactor from putting an unguarded
    payload["pullrequest"]["id"] on the wake path.
    """
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", SECRET)
    code, _ = _post(health, body)
    assert code == 200, f"{label}: webhook must not reject a payload it cannot parse"
    assert m._wake.is_set(), f"{label}: the loop must wake regardless of payload shape"


def test_wake_set_precedes_payload_parsing_in_source():
    """Structural invariant: if the wake happens first, no parsing bug can
    ever suppress it. A source-level assertion survives refactors that a
    behavioural test might quietly stop covering."""
    import inspect
    src = inspect.getsource(m._HealthHandler.do_POST)
    branch = src.split('if event_key.startswith("pullrequest:"):', 1)
    assert len(branch) == 2, "the pullrequest: branch moved; update this test"
    body = branch[1]
    i_wake = body.find("_wake.set()")
    i_parse = body.find("_record_supersede_hint")
    assert i_wake != -1, "_wake.set() vanished from the pullrequest branch"
    assert i_parse != -1, "hint recording is not in the pullrequest branch"
    assert i_wake < i_parse, "_wake.set() must come BEFORE any payload parsing"


# ── 2. hint recording ────────────────────────────────────────────────────

def test_hint_recorded_under_repo_and_pr_id(health, monkeypatch):
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", SECRET)
    _post(health, _payload())
    with m._supersede_lock:
        assert m._pr_superseded.get((REPO, PR_ID)) == NEW_SHA


def test_no_hint_recorded_when_hmac_not_configured(health, monkeypatch):
    """Permissive mode means anyone can POST. An unauthenticated request that
    can abort in-flight renders is a cheap denial of service, so in permissive
    mode we wake (as always) but record nothing."""
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", "")
    code, _ = _post(health, _payload(), secret=None)
    assert code == 200
    assert m._wake.is_set()
    with m._supersede_lock:
        assert m._pr_superseded == {}


def test_display_name_is_not_used_as_the_repo_key(health, monkeypatch):
    """repository.name is a DISPLAY name, repository.full_name carries the
    slug. Keying off the display name would make hints silently never match."""
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", SECRET)
    _post(health, _payload())
    with m._supersede_lock:
        keys = list(m._pr_superseded)
    assert keys == [(REPO, PR_ID)]
    assert not any(k[0] == "Some Display Name" for k in keys)


@pytest.mark.parametrize("event", ["pullrequest:comment_created",
                                   "pullrequest:approved",
                                   "pullrequest:rejected"])
def test_non_push_events_wake_but_record_no_hint(health, monkeypatch, event):
    """Comment/approval events also start with pullrequest: and also embed a
    full pullrequest entity, but their sha is just the current tip."""
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", SECRET)
    code, _ = _post(health, _payload(), event=event)
    assert code == 200 and m._wake.is_set()
    with m._supersede_lock:
        assert m._pr_superseded == {}


def test_relayed_webhook_records_hint_on_the_leader(health, monkeypatch):
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", SECRET)
    code, _ = _post(health, _payload(), extra={"X-ADP-Forwarded": "1"})
    assert code == 200
    with m._supersede_lock:
        assert m._pr_superseded.get((REPO, PR_ID)) == NEW_SHA


# ── 3. arming and the supersede decision ─────────────────────────────────

def test_arm_consumes_a_matching_hint_without_aborting():
    """The webhook that STARTED this iteration leaves a hint whose sha equals
    the one we are about to render. That must not abort the run."""
    sk = (REPO, PR_ID)
    m._record_supersede_hint(REPO, PR_ID, OLD_SHA)
    assert m._arm_supersede(sk, OLD_SHA) is None
    with m._supersede_lock:
        assert sk not in m._pr_superseded


def test_arm_detects_a_hint_that_arrived_while_the_pr_was_queued():
    """The queued case, which a blind clear-on-arm would silently destroy.

    Under MAX_PR_WORKERS=3 a PR can sit queued for minutes after the
    iteration snapshot. A push during that window writes its hint BEFORE
    process_pr arms. Arming is therefore an atomic pop, not a clear.
    """
    sk = (REPO, PR_ID)
    m._record_supersede_hint(REPO, PR_ID, NEW_SHA)
    assert m._arm_supersede(sk, OLD_SHA) == NEW_SHA


def test_superseded_peeks_without_consuming():
    sk = (REPO, PR_ID)
    m._arm_supersede(sk, OLD_SHA)
    m._record_supersede_hint(REPO, PR_ID, NEW_SHA)
    assert m._superseded(sk, OLD_SHA) == NEW_SHA
    assert m._superseded(sk, OLD_SHA) == NEW_SHA, "peek must not consume"


def test_metadata_only_edit_does_not_supersede():
    """pullrequest:updated also fires on title/description/reviewer edits,
    carrying the SAME source.commit.hash."""
    sk = (REPO, PR_ID)
    m._arm_supersede(sk, OLD_SHA)
    m._record_supersede_hint(REPO, PR_ID, OLD_SHA)
    assert m._superseded(sk, OLD_SHA) is None


def test_short_and_full_hashes_compare_equal():
    sk = (REPO, PR_ID)
    m._arm_supersede(sk, OLD_SHA)
    m._record_supersede_hint(REPO, PR_ID, OLD_SHA + "9f8e7d6c5b4a3210ffff")
    assert m._superseded(sk, OLD_SHA) is None, "12-char and 40-char forms must match"


def test_disabled_by_config_never_supersedes(monkeypatch):
    monkeypatch.setattr(m, "SUPERSEDE_ABORT_ENABLED", False)
    sk = (REPO, PR_ID)
    m._record_supersede_hint(REPO, PR_ID, NEW_SHA)
    assert m._arm_supersede(sk, OLD_SHA) is None
    assert m._superseded(sk, OLD_SHA) is None


# ── 4. livelock guard ────────────────────────────────────────────────────

def test_livelock_guard_releases_after_three_consecutive_aborts():
    """A PR pushed to faster than it renders would abort forever and never
    publish anything. After 3 consecutive aborts, let the run finish."""
    sk = (REPO, PR_ID)
    for i in range(m.SUPERSEDE_MAX_CONSECUTIVE_ABORTS):
        m._arm_supersede(sk, OLD_SHA)
        m._record_supersede_hint(REPO, PR_ID, NEW_SHA)
        assert m._superseded(sk, OLD_SHA) == NEW_SHA, f"abort {i + 1} should fire"
        m._note_supersede_abort(sk)
    m._arm_supersede(sk, OLD_SHA)
    m._record_supersede_hint(REPO, PR_ID, NEW_SHA)
    assert m._superseded(sk, OLD_SHA) is None, "guard must release after the cap"


def test_completing_normally_resets_the_abort_counter():
    sk = (REPO, PR_ID)
    for _ in range(m.SUPERSEDE_MAX_CONSECUTIVE_ABORTS):
        m._note_supersede_abort(sk)
    m._note_supersede_complete(sk)
    m._arm_supersede(sk, OLD_SHA)
    m._record_supersede_hint(REPO, PR_ID, NEW_SHA)
    assert m._superseded(sk, OLD_SHA) == NEW_SHA


# ── 5. state hygiene ─────────────────────────────────────────────────────

def test_pruning_drops_state_for_prs_no_longer_open():
    """Otherwise the dict grows one entry per force-pushed-then-closed PR,
    forever, for the life of the pod."""
    m._record_supersede_hint(REPO, 1, "aaaaaaaaaaaa")
    m._record_supersede_hint(REPO, 2, "bbbbbbbbbbbb")
    m._note_supersede_abort((REPO, 1))
    m._note_supersede_abort((REPO, 2))
    m._prune_supersede_state({(REPO, 2)})
    with m._supersede_lock:
        assert (REPO, 1) not in m._pr_superseded
        assert (REPO, 2) in m._pr_superseded
        assert (REPO, 1) not in m._pr_supersede_aborts
        assert (REPO, 2) in m._pr_supersede_aborts


# ── 6. counters ──────────────────────────────────────────────────────────

def test_stats_endpoint_exposes_bitbucket_webhook_counters(health, monkeypatch):
    """A silently dead webhook (deleted in Bitbucket, ingress dropping the
    POST, secret drifted after a rotation) is invisible today: the code is
    correct and the service quietly runs on the 60s poll."""
    monkeypatch.setattr(m, "BB_WEBHOOK_SECRET", SECRET)
    _post(health, _payload())
    req = urllib.request.Request(f"{health}/diff-preview/stats")
    with urllib.request.urlopen(req, timeout=10) as r:
        payload = json.loads(r.read())
    bb = payload.get("bb_webhook")
    assert bb is not None, "/diff-preview/stats must expose bb_webhook counters"
    for key in ("received", "rejected_hmac", "rejected_format", "wakes",
                "hints_recorded", "supersedes_triggered", "last_received_at"):
        assert key in bb, f"missing counter: {key}"
    assert bb["received"] >= 1 and bb["wakes"] >= 1 and bb["hints_recorded"] >= 1


# ── 7. the real orchestrator, not just the helpers ───────────────────────
# These drive the actual process_pr against the synthetic PR world, which is
# what proves the abort semantics rather than the bookkeeping around them.

from test_coverage_orchestration import world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA  # noqa: E402,F401

ORCH_KEY = (m.BB_REPO, 991)


def test_process_pr_publishes_nothing_when_superseded_before_it_starts(world):
    """The queued case end to end: comment, status and _seen all untouched."""
    sinks, _plan = world
    m._record_supersede_hint(m.BB_REPO, 991, "ffffffffffff")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)

    assert sinks.upserts == [], "a dead commit must never overwrite the comment"
    assert sinks.statuses == [], "no build status for a superseded commit"
    assert sinks.diff_calls == [], "no app should have been rendered at all"
    with m._seen_lock:
        assert ORCH_KEY not in m._seen, "_seen must stay unset so the PR is retried"


def test_process_pr_mid_render_supersede_discards_the_whole_result(world):
    """A push landing while the batch runs: the finished diffs are thrown
    away rather than published against the dead commit."""
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")

    real_diff = m.argocd_diff

    def diff_then_push(app, pr_sha, main_sha, **kw):
        # Simulate the second force-push arriving mid-render.
        m._record_supersede_hint(m.BB_REPO, 991, "ffffffffffff")
        return real_diff(app, pr_sha, main_sha, **kw)

    m.argocd_diff = diff_then_push
    try:
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    finally:
        m.argocd_diff = real_diff

    assert sinks.upserts == [], "superseded render must not publish a comment"
    assert "SUCCESSFUL" not in [s for s, _ in sinks.statuses]
    with m._seen_lock:
        assert ORCH_KEY not in m._seen


def test_process_pr_is_untouched_when_nothing_supersedes(world):
    """The overwhelmingly common single-push case must behave exactly as
    before: comment posted, status green, _seen set."""
    sinks, plan = world
    plan["pv-orch-a-ms"] = m.DiffResult(
        "--- main\n+++ pr", [("Deployment/webx", "-replicas: 2\n+replicas: 3")],
        1, True, "", m.OUT_DIFF, "")
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)

    assert len(sinks.upserts) == 1
    assert [s for s, _ in sinks.statuses][-1] == "SUCCESSFUL"
    with m._seen_lock:
        assert ORCH_KEY in m._seen


def test_a_matching_hint_does_not_abort_the_run_it_belongs_to(world):
    """The webhook that STARTED this iteration must not kill it."""
    sinks, _plan = world
    m._record_supersede_hint(m.BB_REPO, 991, PR_SHA)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    assert len(sinks.upserts) == 1, "same-sha hint must be consumed, not acted on"


def test_completing_a_run_clears_the_abort_streak(world):
    _sinks, _plan = world
    m._note_supersede_abort(ORCH_KEY)
    m._note_supersede_abort(ORCH_KEY)
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    with m._supersede_lock:
        assert ORCH_KEY not in m._pr_supersede_aborts
