"""v2.5.10 — prominent WARNING for a full environment decommission.

Explicit request (Marcos): when an environment is genuinely deleted (not
moved, not rebuilt under a new name — the identity file customer.yaml/
config.yaml disappears with no successor anywhere), the PR comment must
shout it: WHICH environment, WHAT version it was on, and a (possibly
truncated) list of what will be removed. Only for this exact scenario —
not for a tier move (v2.5.8), not for a rebuild under a different name
(v2.5.9's rename-identity fix already keeps that path honest), and not for
a partial change within a still-existing customer.yaml.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


ENV_DIR   = "gcp/qa/private-cloud/ap1/custom/pv-qa-decom-a"
IDENTITY  = f"{ENV_DIR}/customer.yaml"
ANCILLARY = f"{ENV_DIR}/cicd-versions.yaml"

PATH_MAP = {
    IDENTITY:  ["pv-qa-decom-a-ms", "pv-qa-decom-a-ss"],
    ANCILLARY: ["pv-qa-decom-a-ms", "pv-qa-decom-a-ss"],
    "gcp/qa/config.yaml": ["pv-qa-decom-a-ms", "pv-qa-decom-a-ss", "unrelated-other-app"],
}


# ── detection (pure, no network) ─────────────────────────────────────────

def test_detect_decommission_candidate_basic():
    changed = [IDENTITY, ANCILLARY]
    candidates = m._detect_env_decommission_candidates(changed, PATH_MAP, renames={})
    assert len(candidates) == 1
    c = candidates[0]
    assert c["env_name"] == "pv-qa-decom-a"
    assert c["identity_file"] == IDENTITY
    assert sorted(c["apps"]) == ["pv-qa-decom-a-ms", "pv-qa-decom-a-ss"]


def test_detect_decommission_ignores_real_move():
    # If the identity file itself has a rename entry, it's a MOVE (v2.5.8
    # territory), not a decommission — must not double-report.
    new_dir = "gcp/qa/private-cloud/ap1/monthly/pv-qa-decom-a"
    renames = {IDENTITY: f"{new_dir}/customer.yaml"}
    candidates = m._detect_env_decommission_candidates([IDENTITY], PATH_MAP, renames)
    assert candidates == []


def test_detect_decommission_ignores_ancillary_only_change():
    # Only cicd-versions.yaml changed (customer.yaml untouched) — normal
    # change, not a decommission.
    candidates = m._detect_env_decommission_candidates([ANCILLARY], PATH_MAP, renames={})
    assert candidates == []


def test_detect_decommission_ignores_file_not_in_path_map():
    # A customer.yaml path that ISN'T a currently-live app (e.g. it's part
    # of a brand-new environment being added) is the new-env path, not
    # decommission.
    candidates = m._detect_env_decommission_candidates(
        ["gcp/qa/private-cloud/ap1/custom/pv-brand-new-a/customer.yaml"],
        PATH_MAP, renames={})
    assert candidates == []


def test_detect_decommission_ignores_shared_ancestor_default():
    # gcp/qa/config.yaml is a shared default (basename config.yaml) mapped
    # to MANY apps across MANY environments — deleting/editing it is never
    # a single-environment decommission on its own.
    candidates = m._detect_env_decommission_candidates(
        ["gcp/qa/config.yaml"], PATH_MAP, renames={})
    assert candidates == []


# ── evaluation (network + render, mocked) ────────────────────────────────

def _mk_resources(names):
    return {("Deployment", "ns", n): f"kind: Deployment\nmetadata:\n  name: {n}\n"
            for n in names}


def test_evaluate_decommissions_confirms_deletion_before_firing(monkeypatch):
    candidate = {"env_name": "pv-qa-decom-a", "identity_file": IDENTITY,
                 "apps": ["pv-qa-decom-a-ms"]}

    def fake_bb_fetch_status(clean, sha):
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    monkeypatch.setitem(m._app_chart_revision_map, "pv-qa-decom-a-ms", "2603.0.1-dev")
    monkeypatch.setattr(m, "_render_main_side_resources",
        lambda app, main_sha: _mk_resources(["account", "api-gateway"]))
    m._vf_cache.clear()

    lines, envs = m._evaluate_env_decommissions([candidate], "prsha000", "mainsha000")
    body = "\n".join(lines)
    assert envs == ["pv-qa-decom-a"]
    assert "DECOMMISSION" in body
    assert "pv-qa-decom-a" in body
    assert "2603.0.1-dev" in body
    assert "account" in body


def test_evaluate_decommissions_skips_if_not_actually_deleted(monkeypatch):
    # Defensive guard: if the identity file is still fetchable at pr_sha
    # (not genuinely gone — maybe a transient Bitbucket hiccup, or this got
    # called on a false positive), do NOT fire a decommission warning.
    candidate = {"env_name": "pv-qa-decom-a", "identity_file": IDENTITY,
                 "apps": ["pv-qa-decom-a-ms"]}

    def fake_bb_fetch_status(clean, sha):
        return "appspace:\n  version: x\n", m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()

    lines, envs = m._evaluate_env_decommissions([candidate], "prsha000", "mainsha000")
    assert lines == [] and envs == []


def test_evaluate_decommissions_aggregates_across_apps(monkeypatch):
    # glb/ms/ss share one environment — the warning should report one
    # combined block, not three, aggregating resource counts.
    candidate = {"env_name": "pv-qa-decom-a", "identity_file": IDENTITY,
                 "apps": ["pv-qa-decom-a-ms", "pv-qa-decom-a-ss"]}
    monkeypatch.setattr(m, "_bb_fetch_status", lambda clean, sha: (None, m.BB_NOT_FOUND))
    monkeypatch.setitem(m._app_chart_revision_map, "pv-qa-decom-a-ms", "2603.0.1-dev")
    monkeypatch.setitem(m._app_chart_revision_map, "pv-qa-decom-a-ss", "2603.0.1-dev")

    def fake_render(app, main_sha):
        if app == "pv-qa-decom-a-ms":
            return _mk_resources(["account", "api-gateway"])
        return _mk_resources(["ping-scaler"])
    monkeypatch.setattr(m, "_render_main_side_resources", fake_render)
    m._vf_cache.clear()

    lines, envs = m._evaluate_env_decommissions([candidate], "prsha000", "mainsha000")
    body = "\n".join(lines)
    assert "account" in body and "api-gateway" in body and "ping-scaler" in body
    # One heading, not three.
    assert body.count("DECOMMISSION") == 1


def test_evaluate_decommissions_best_effort_when_render_fails(monkeypatch):
    # A render failure must not suppress the warning entirely — the
    # deletion itself is the important, confirmed fact. Degrade gracefully.
    candidate = {"env_name": "pv-qa-decom-a", "identity_file": IDENTITY,
                 "apps": ["pv-qa-decom-a-ms"]}
    monkeypatch.setattr(m, "_bb_fetch_status", lambda clean, sha: (None, m.BB_NOT_FOUND))
    monkeypatch.setitem(m._app_chart_revision_map, "pv-qa-decom-a-ms", "2603.0.1-dev")
    monkeypatch.setattr(m, "_render_main_side_resources",
        lambda app, main_sha: (_ for _ in ()).throw(RuntimeError("chart pull failed")))
    m._vf_cache.clear()

    lines, envs = m._evaluate_env_decommissions([candidate], "prsha000", "mainsha000")
    body = "\n".join(lines)
    assert envs == ["pv-qa-decom-a"]
    assert "DECOMMISSION" in body
    assert "2603.0.1-dev" in body


def test_evaluate_decommissions_truncates_long_resource_list(monkeypatch):
    candidate = {"env_name": "pv-qa-decom-a", "identity_file": IDENTITY,
                 "apps": ["pv-qa-decom-a-ms"]}
    monkeypatch.setattr(m, "_bb_fetch_status", lambda clean, sha: (None, m.BB_NOT_FOUND))
    monkeypatch.setitem(m._app_chart_revision_map, "pv-qa-decom-a-ms", "2603.0.1-dev")
    many = [f"svc-{i}" for i in range(80)]
    monkeypatch.setattr(m, "_render_main_side_resources",
        lambda app, main_sha: _mk_resources(many))
    m._vf_cache.clear()

    lines, envs = m._evaluate_env_decommissions([candidate], "prsha000", "mainsha000")
    body = "\n".join(lines)
    assert "more" in body.lower() or "truncat" in body.lower()
    assert len(body) < 8000


# ── format_comment wiring ────────────────────────────────────────────────

def test_format_comment_splices_decommission_block():
    decommission_lines = ["# \U0001f5d1\ufe0f\u26a0\ufe0f ENVIRONMENT DECOMMISSION \u26a0\ufe0f\U0001f5d1\ufe0f", "",
                           "`pv-qa-decom-a` (was on `2603.0.1-dev`) is being deleted."]
    body = m.format_comment("deadbeef01234567", {}, decommission_lines=decommission_lines)
    assert "DECOMMISSION" in body
    assert "pv-qa-decom-a" in body


def test_format_comment_no_decommission_block_by_default():
    body = m.format_comment("deadbeef01234567", {})
    assert "DECOMMISSION" not in body


# ── v2.5.11: a CONFIRMED decommission must not surface as a build failure ──

# Live finding (PR #6677 on acme-config-dev): a confirmed environment
# decommission's own apps (glb/ms/ss) fell through the NORMAL diff pipeline,
# failed to render (their customer.yaml is genuinely gone), and landed as
# OUT_INDETERMINATE/render_failed. That is a RETRYABLE, non-permanent
# outcome, so the PR was never marked "seen": the pod re-diffed it forever,
# re-attempting a render that can never succeed, and the build status said
# "Diff incomplete... NOT confirmed unchanged (will retry automatically)" —
# actively misleading once the decommission warning has ALREADY confirmed
# and explained exactly what is happening. A confirmed decommission is not
# an error to retry; it is a settled, understood fact.

def test_apps_to_skip_for_decommission_returns_confirmed_apps_only():
    candidates = [
        {"env_name": "pv-qa-14-a", "identity_file": "x", "apps": ["pv-qa-14-a-ms", "pv-qa-14-a-ss"]},
        {"env_name": "pv-qa-99-a", "identity_file": "y", "apps": ["pv-qa-99-a-ms"]},
    ]
    # Only pv-qa-14-a was actually confirmed deleted (BB_NOT_FOUND);
    # pv-qa-99-a's candidate existed structurally but was never confirmed
    # (e.g. a false positive, or a transient fetch issue) — its apps must
    # still go through the normal diff pipeline, not be silently skipped.
    confirmed_envs = ["pv-qa-14-a"]
    apps = m._apps_to_skip_for_decommission(candidates, confirmed_envs)
    assert apps == {"pv-qa-14-a-ms", "pv-qa-14-a-ss"}


def test_apps_to_skip_for_decommission_empty_when_nothing_confirmed():
    candidates = [{"env_name": "pv-qa-14-a", "identity_file": "x", "apps": ["pv-qa-14-a-ms"]}]
    assert m._apps_to_skip_for_decommission(candidates, []) == set()


def test_format_comment_renders_decommissioned_app_specially():
    result = m.DiffResult("", [], 0, False, None, m.OUT_DECOMMISSIONED, "confirmed_decommission")
    body = m.format_comment("deadbeef01234567", {"pv-qa-14-a-ms": result})
    assert "diff unavailable" not in body.lower()
    assert "decommission" in body.lower()
    assert "pv-qa-14-a-ms" in body


def test_out_decommissioned_is_not_permanent_or_retryable_reason():
    # Its reason must be excluded from BOTH buckets: not permanent (would
    # force a FAILED/blocked status like oci_not_found does) and not
    # retryable (would keep it un-seen and re-diffed forever).
    assert "confirmed_decommission" not in m.PERMANENT_REASONS
    assert "confirmed_decommission" not in m.RETRYABLE_REASONS
