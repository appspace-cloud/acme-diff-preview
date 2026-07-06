"""Regression tests for v2.5.15 -- Finding 7: identity-aware rename following.

Bitbucket's rename detection is content-similarity based, not identity
based. Before this fix, ANY rename of customer.yaml/config.yaml was trusted
unconditionally as "direct evidence" of a real environment move -- correct
for a folder-name-to-suffix path fix, but wrong for a decommission+rebuild
or region migration under a new suffix, which real acme-config-prod history
shows Bitbucket pairs anyway (49 real cases mined from 600 commits, see
bughunt/FINDINGS_IDENTITY_AWARE_RENAME.md).

Fixtures below mirror the real prod cases directly:
  - Class 1 (same customerName+suffix, folder path fix): pv-allianzna-a ->
    pv-allianzna-c ("Rename folders" commit 655546c96) -- must stay trusted.
  - Class 2 (customerName or suffix CHANGED): pv-manulife-a -> pv-manulife-b
    (suffix a->b), pv-seagal-a -> pv-segal-a (customerName typo fix, same
    suffix) -- must be refused.
"""
import os
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


def setup_function(_):
    m._identity_rename_verdict_cache.clear()
    m._vf_cache.clear()
    m._vf_inflight.clear()


# ── _extract_appspace_identity ────────────────────────────────────────────

def test_extract_appspace_identity_basic():
    content = (
        "appspace:\n"
        "  hostingID: \"00000169\"\n"
        "  customerName: manulife\n"
        "  suffix: b\n"
        "  instanceName: pv-manulife-b\n"
    )
    assert m._extract_appspace_identity(content) == ("manulife", "b")


def test_extract_appspace_identity_missing_suffix():
    # suffix often inherited from a parent config.yaml, not stated here.
    content = "appspace:\n  customerName: onr\n"
    assert m._extract_appspace_identity(content) == ("onr", None)


def test_extract_appspace_identity_quoted_values():
    content = "appspace:\n  customerName: 'takeda'\n  suffix: \"b\"\n"
    assert m._extract_appspace_identity(content) == ("takeda", "b")


def test_extract_appspace_identity_ignores_deeper_nested_keys():
    # A customerName/suffix nested under something else (not a direct child
    # of appspace:) must not be mistaken for the real one, mirroring the
    # same direct-child tracking _extract_chart_version_checked uses.
    content = (
        "appspace:\n"
        "  customerName: real\n"
        "  microservices:\n"
        "    definitions:\n"
        "      customerName: decoy\n"
        "      suffix: decoy\n"
    )
    assert m._extract_appspace_identity(content) == ("real", None)


def test_extract_appspace_identity_empty_content():
    assert m._extract_appspace_identity("") == (None, None)
    assert m._extract_appspace_identity(None) == (None, None)


# ── _same_env_identity ────────────────────────────────────────────────────

def test_same_env_identity_class1_allianzna_path_fix():
    # Real prod case: folder pv-allianzna-a -> pv-allianzna-c, internal
    # (customerName, suffix) already ('allianzna', 'c') on BOTH sides.
    assert m._same_env_identity(("allianzna", "c"), ("allianzna", "c")) is True


def test_same_env_identity_class2_manulife_suffix_changed():
    # Real prod case: pv-manulife-a -> pv-manulife-b, suffix a -> b inside.
    assert m._same_env_identity(("manulife", "a"), ("manulife", "b")) is False


def test_same_env_identity_class2_seagal_customername_typo_fix():
    # Real prod case: pv-seagal-a -> pv-segal-a, SAME suffix, but
    # customerName itself differs -- still a different identity.
    assert m._same_env_identity(("seagal", "a"), ("segal", "a")) is False


def test_same_env_identity_class2_bnym_customername_changed():
    # Real prod case: pv-bnym--aec1-b -> pv-bny--aec1-b.
    assert m._same_env_identity(("bnym--aec1", "b"), ("bny--aec1", "b")) is False


def test_same_env_identity_unknown_degrades_to_trust():
    # Both sides unparseable (fetch failure, unusual format) -- conservative
    # default is to trust, not block on noise (matches _is_version_downgrade).
    assert m._same_env_identity((None, None), (None, None)) is True


def test_same_env_identity_undeclared_suffix_does_not_false_positive():
    # suffix inherited (undeclared locally) on one or both sides must not
    # be read as a mismatch -- only customerName is compared then.
    assert m._same_env_identity(("onr", None), ("onr", None)) is True
    assert m._same_env_identity(("onr", "b"), ("onr", None)) is True


# ── _rename_identity_confirmed (with mocked _bb_fetch_status) ─────────────

MANULIFE_OLD = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-a/customer.yaml"
MANULIFE_NEW = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-b/customer.yaml"
ALLIANZNA_OLD = "azure/prod/private-cloud/na1-a/monthly/pv-allianzna-a/customer.yaml"
ALLIANZNA_NEW = "azure/prod/private-cloud/na1-a/monthly/pv-allianzna-c/customer.yaml"


def _fake_fetch_manulife(clean, sha):
    if clean == MANULIFE_OLD:
        return "appspace:\n  customerName: manulife\n  suffix: a\n", m.BB_OK
    if clean == MANULIFE_NEW:
        return "appspace:\n  customerName: manulife\n  suffix: b\n", m.BB_OK
    return None, m.BB_NOT_FOUND


def _fake_fetch_allianzna(clean, sha):
    if clean == ALLIANZNA_OLD:
        return "appspace:\n  customerName: allianzna\n  suffix: c\n", m.BB_OK
    if clean == ALLIANZNA_NEW:
        return "appspace:\n  customerName: allianzna\n  suffix: c\n", m.BB_OK
    return None, m.BB_NOT_FOUND


def test_rename_identity_confirmed_rejects_class2(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_manulife)
    assert m._rename_identity_confirmed(
        MANULIFE_OLD, MANULIFE_NEW, "mainsha1", "prsha1") is False


def test_rename_identity_confirmed_accepts_class1(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_allianzna)
    assert m._rename_identity_confirmed(
        ALLIANZNA_OLD, ALLIANZNA_NEW, "mainsha2", "prsha2") is True


def test_rename_identity_confirmed_is_memoized(monkeypatch):
    calls = {"n": 0}

    def counting_fetch(clean, sha):
        calls["n"] += 1
        return _fake_fetch_manulife(clean, sha)

    monkeypatch.setattr(m, "_bb_fetch_status", counting_fetch)
    r1 = m._rename_identity_confirmed(MANULIFE_OLD, MANULIFE_NEW, "s1", "s2")
    r2 = m._rename_identity_confirmed(MANULIFE_OLD, MANULIFE_NEW, "s1", "s2")
    assert r1 is False and r2 is False
    assert calls["n"] == 2, "second call must be served from cache, not refetched"


def test_rename_identity_confirmed_fetch_failure_degrades_to_trust(monkeypatch):
    def failing_fetch(clean, sha):
        raise RuntimeError("simulated Bitbucket outage")
    monkeypatch.setattr(m, "_bb_fetch_status", failing_fetch)
    assert m._rename_identity_confirmed(
        MANULIFE_OLD, MANULIFE_NEW, "s3", "s4") is True


# ── _detect_env_move ──────────────────────────────────────────────────────

def test_detect_env_move_rejects_class2_manulife(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_manulife)
    old_dir = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-a"
    new_dir = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-b"
    value_files = [f"$config/{MANULIFE_OLD}", f"$config/{old_dir}/cicd-versions.yaml"]
    renames = {MANULIFE_OLD: MANULIFE_NEW}
    result = m._detect_env_move(value_files, renames, "mainsha5", "prsha5")
    assert result is None, (
        "a rename with a changed customerName/suffix must not be trusted "
        "as a move of THIS environment")


def test_detect_env_move_confirms_class1_allianzna(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_allianzna)
    old_dir = "azure/prod/private-cloud/na1-a/monthly/pv-allianzna-a"
    new_dir = "azure/prod/private-cloud/na1-a/monthly/pv-allianzna-c"
    value_files = [f"$config/{ALLIANZNA_OLD}"]
    renames = {ALLIANZNA_OLD: ALLIANZNA_NEW}
    result = m._detect_env_move(value_files, renames, "mainsha6", "prsha6")
    assert result == (old_dir, new_dir)


def test_detect_env_move_without_shas_keeps_legacy_trust(monkeypatch):
    # Regression guard: omitting main_sha/pr_sha (legacy call sites, and
    # every pre-v2.5.15 test in test_v258_tier_move_downgrade.py) must keep
    # the old unconditional-trust-on-path behavior, even for a pairing that
    # WOULD fail the identity check if shas were supplied.
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_manulife)
    old_dir = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-a"
    new_dir = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-b"
    value_files = [f"$config/{MANULIFE_OLD}"]
    renames = {MANULIFE_OLD: MANULIFE_NEW}
    result = m._detect_env_move(value_files, renames)
    assert result == (old_dir, new_dir)


# ── _is_trusted_rename / _trusted_rename_dirs ─────────────────────────────

def test_is_trusted_rename_rejects_class2_with_shas(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_manulife)
    assert m._is_trusted_rename(
        MANULIFE_OLD, MANULIFE_NEW, set(), "mainsha7", "prsha7") is False


def test_is_trusted_rename_accepts_class1_with_shas(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_allianzna)
    assert m._is_trusted_rename(
        ALLIANZNA_OLD, ALLIANZNA_NEW, set(), "mainsha8", "prsha8") is True


def test_is_trusted_rename_legacy_without_shas_still_trusts():
    # No monkeypatch needed: without shas the function never calls
    # _bb_fetch_status at all (pure path check, pre-v2.5.15 behavior).
    assert m._is_trusted_rename(MANULIFE_OLD, MANULIFE_NEW, set()) is True


def test_trusted_rename_dirs_excludes_class2_pair(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_manulife)
    old_dir = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-a"
    new_dir = "gcp/prod/private-cloud/na1-b/monthly/pv-manulife-b"
    renames = {MANULIFE_OLD: MANULIFE_NEW}
    trusted = m._trusted_rename_dirs(renames, "mainsha9", "prsha9")
    assert (old_dir, new_dir) not in trusted, (
        "a Class 2 identity-file pairing must not corroborate ancillary "
        "files renamed between the same two directories either")


def test_trusted_rename_dirs_keeps_class1_pair(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_allianzna)
    old_dir = "azure/prod/private-cloud/na1-a/monthly/pv-allianzna-a"
    new_dir = "azure/prod/private-cloud/na1-a/monthly/pv-allianzna-c"
    renames = {ALLIANZNA_OLD: ALLIANZNA_NEW}
    trusted = m._trusted_rename_dirs(renames, "mainsha10", "prsha10")
    assert (old_dir, new_dir) in trusted


# ── _pr_chart_revision_checked ────────────────────────────────────────────

def test_pr_chart_revision_checked_does_not_adopt_class2_version(monkeypatch):
    # Live-probe repro: an -a app must not silently adopt the unrelated -b
    # environment's appspace.version just because Bitbucket paired the
    # customer.yaml files by content similarity.
    app = "pv-manulife-a-ms"
    m._app_chart_revision_map[app] = "2600.1.0-dev"

    def fake_fetch(clean, sha):
        if clean == MANULIFE_NEW:
            return ("appspace:\n  customerName: manulife\n  suffix: b\n"
                    "  version: 2699.9.9-dev\n", m.BB_OK)
        if clean == MANULIFE_OLD:
            return "appspace:\n  customerName: manulife\n  suffix: a\n", m.BB_OK
        return None, m.BB_NOT_FOUND

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    new_rev, invalid = m._pr_chart_revision_checked(
        app, [MANULIFE_OLD], "prsha11", main_sha="mainsha11",
        renames={MANULIFE_OLD: MANULIFE_NEW})
    assert new_rev is None, (
        f"must not adopt the unrelated -b environment's version, got {new_rev!r}")
    assert invalid is False


def test_pr_chart_revision_checked_still_follows_class1_rename(monkeypatch):
    # Regression guard: a real path-fix move (Finding 6's original case)
    # must keep working after the identity check is added.
    app = "pv-allianzna-c-ms"
    m._app_chart_revision_map[app] = "1.0.0"

    def fake_fetch(clean, sha):
        if clean == ALLIANZNA_NEW and sha == "prsha12":
            return ("appspace:\n  customerName: allianzna\n  suffix: c\n"
                    "  version: 2.0.0\n", m.BB_OK)
        if clean == ALLIANZNA_OLD and sha == "mainsha12":
            return "appspace:\n  customerName: allianzna\n  suffix: c\n", m.BB_OK
        return None, m.BB_NOT_FOUND

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    new_rev, invalid = m._pr_chart_revision_checked(
        app, [ALLIANZNA_OLD], "prsha12", main_sha="mainsha12",
        renames={ALLIANZNA_OLD: ALLIANZNA_NEW})
    assert new_rev == "2.0.0"
    assert invalid is False


# ── _run_one_diff (full glue) ──────────────────────────────────────────────

def test_run_one_diff_class2_rename_does_not_leak_wrong_content(monkeypatch):
    # Full-path repro: the LIVE ArgoCD app pv-manulife-a-ms still points at
    # the OLD folder (that's how ArgoCD is configured pre-merge). Bitbucket
    # pairs customer.yaml with the unrelated pv-manulife-b (content-similar,
    # real prod shape) as a rename. Before v2.5.15 this rebased the PR-side
    # render onto -b's content; after the fix it must not.
    app = "test-app-run-one-diff-class2"
    value_files = [MANULIFE_OLD]

    monkeypatch.setitem(m._app_chart_map, app, "appspace-micro-services")
    monkeypatch.setitem(m._app_chart_revision_map, app, "2600.1.0-dev")
    monkeypatch.setitem(m._app_chart_registry_map, app, "helm-oci-dev.repo.appspace.com")
    monkeypatch.setitem(m._app_value_files_map, app, value_files)
    monkeypatch.setitem(m._app_namespace_map, app, "pv-manulife-a")
    monkeypatch.setattr(m, "_ensure_chart", lambda registry, chart, ver: "/fake/chart/path")

    captured = {}
    def fake_helm_template(chart_path, release, namespace, value_files_content):
        captured.setdefault("calls", []).append(dict(value_files_content))
        return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", None
    monkeypatch.setattr(m, "_helm_template", fake_helm_template)

    def fake_bb_fetch_status(clean, sha):
        if clean == MANULIFE_OLD and sha == "mainsha13":
            return "appspace:\n  customerName: manulife\n  suffix: a\n", m.BB_OK
        if clean == MANULIFE_OLD and sha == "prsha13":
            return None, m.BB_NOT_FOUND  # moved away, gone at pr_sha
        if clean == MANULIFE_NEW and sha == "prsha13":
            return ("appspace:\n  customerName: manulife\n  suffix: b\n"
                    "  version: 2699.9.9-dev\n  MARKER: unrelated_b_content\n"), m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()
    m._vf_inflight.clear()
    m._identity_rename_verdict_cache.clear()

    m._run_one_diff(
        app, pr_sha="prsha13", main_sha="mainsha13",
        changed_paths=[MANULIFE_OLD, MANULIFE_NEW],
        renames={MANULIFE_OLD: MANULIFE_NEW},
    )
    all_content = "".join(str(c) for call in captured.get("calls", []) for c in call.values())
    assert "unrelated_b_content" not in all_content, (
        "the unrelated pv-manulife-b environment's content leaked into the "
        "pv-manulife-a app's PR-side render")
    assert "2699.9.9-dev" not in all_content


def test_run_one_diff_class1_rename_still_follows(monkeypatch):
    # Regression guard: the real T2b/Finding-6 case (same identity, folder
    # path fix) must keep rendering with the new location's content.
    app = "test-app-run-one-diff-class1"
    value_files = [ALLIANZNA_OLD]

    monkeypatch.setitem(m._app_chart_map, app, "appspace-micro-services")
    monkeypatch.setitem(m._app_chart_revision_map, app, "1.0.0")
    monkeypatch.setitem(m._app_chart_registry_map, app, "helm-oci-dev.repo.appspace.com")
    monkeypatch.setitem(m._app_value_files_map, app, value_files)
    monkeypatch.setitem(m._app_namespace_map, app, "pv-allianzna-a")
    monkeypatch.setattr(m, "_ensure_chart", lambda registry, chart, ver: "/fake/chart/path")

    captured = {}
    def fake_helm_template(chart_path, release, namespace, value_files_content):
        captured.setdefault("calls", []).append(dict(value_files_content))
        return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", None
    monkeypatch.setattr(m, "_helm_template", fake_helm_template)

    def fake_bb_fetch_status(clean, sha):
        if clean == ALLIANZNA_OLD and sha == "mainsha14":
            return "appspace:\n  customerName: allianzna\n  suffix: c\n", m.BB_OK
        if clean == ALLIANZNA_OLD and sha == "prsha14":
            return None, m.BB_NOT_FOUND
        if clean == ALLIANZNA_NEW and sha == "prsha14":
            return ("appspace:\n  customerName: allianzna\n  suffix: c\n"
                    "  MARKER: moved_content\n"), m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()
    m._vf_inflight.clear()
    m._identity_rename_verdict_cache.clear()

    m._run_one_diff(
        app, pr_sha="prsha14", main_sha="mainsha14",
        changed_paths=[ALLIANZNA_OLD, ALLIANZNA_NEW],
        renames={ALLIANZNA_OLD: ALLIANZNA_NEW},
    )
    all_content = "".join(str(c) for call in captured.get("calls", []) for c in call.values())
    assert "moved_content" in all_content, (
        "a legitimate same-identity path-fix rename must still follow "
        "through to the new location's content")


# ── _detect_env_decommission_candidates falls through on rejection ────────

def test_decommission_candidates_sees_old_env_after_class2_rejection(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_manulife)
    path_map = {
        MANULIFE_OLD: ["pv-manulife-a-ms", "pv-manulife-a-ss"],
    }
    changed = [MANULIFE_OLD, MANULIFE_NEW]
    renames = {MANULIFE_OLD: MANULIFE_NEW}
    candidates = m._detect_env_decommission_candidates(
        changed, path_map, renames, main_sha="mainsha15", pr_sha="prsha15")
    assert len(candidates) == 1, (
        "a Class 2 rejected rename must still surface the OLD environment "
        "as a decommission candidate, or the reviewer gets no warning at all")
    assert candidates[0]["env_name"] == "pv-manulife-a"
    assert sorted(candidates[0]["apps"]) == ["pv-manulife-a-ms", "pv-manulife-a-ss"]


def test_decommission_candidates_excludes_old_env_for_class1_move(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status", _fake_fetch_allianzna)
    path_map = {
        ALLIANZNA_OLD: ["pv-allianzna-a-ms"],
    }
    changed = [ALLIANZNA_OLD, ALLIANZNA_NEW]
    renames = {ALLIANZNA_OLD: ALLIANZNA_NEW}
    candidates = m._detect_env_decommission_candidates(
        changed, path_map, renames, main_sha="mainsha16", pr_sha="prsha16")
    assert candidates == [], (
        "a confirmed same-identity move must NOT be reported as a "
        "decommission of the old path")
