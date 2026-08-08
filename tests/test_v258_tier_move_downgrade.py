"""v2.5.8 — tier-move false clean (T2b) + chart downgrade warning.

Live finding (PR #6666 on acme-config-dev, mirror of prod #3597): moving an
environment folder between tiers (custom/ -> monthly/) with the version left
to the tier default rendered "No manifest changes" while the merge would
actually change the chart version (2602.2.11-dev -> 2601.4.15-dev tier
default). Root cause: the app's valueFiles carry relative parent references
(<env>/../config.yaml -> tier defaults) that keep resolving to the OLD tier
after a move — the old tier file still exists, so the fetch silently succeeds
with the wrong defaults and both diff sides render identically.

Also adds a prominent downgrade warning: when the PR-side chart version is
LOWER than the current one, the comment must shout it and the build status
must mention it.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m

# COPS-2612: these cases exercise the INLINE diff-block rendering
# path. The comment stopped using it by default when phase E flipped
# COMMENT_INLINE_DIFFS, but it is still what the full-diff page renders
# always and what the comment renders on rollback, so the behaviour
# below (redaction, body caps, fence safety) must keep being tested.
_INLINE = m.COMMENT_PROFILE.replace(inline_diffs=True)



OLD_DIR = "gcp/qa/private-cloud/ap1/custom/pv-qa-13-a"
NEW_DIR = "gcp/qa/private-cloud/ap1/monthly/pv-qa-13-a"

VALUE_FILES = [
    "$config/gcp/config.yaml",
    "$config/gcp/qa/config.yaml",
    "$config/gcp/qa/private-cloud/config.yaml",
    f"$config/{OLD_DIR}/../../config.yaml",
    f"$config/{OLD_DIR}/../config.yaml",
    f"$config/{OLD_DIR}/customer.yaml",
    f"$config/{OLD_DIR}/cicd-versions.yaml",
]

RENAMES = {
    f"{OLD_DIR}/customer.yaml":      f"{NEW_DIR}/customer.yaml",
    f"{OLD_DIR}/cicd-versions.yaml": f"{NEW_DIR}/cicd-versions.yaml",
}


# ── env-move detection ──────────────────────────────────────────────────

def test_detect_env_move_from_renames():
    move = m._detect_env_move(VALUE_FILES, RENAMES)
    assert move == (OLD_DIR, NEW_DIR)


def test_detect_env_move_none_without_renames():
    assert m._detect_env_move(VALUE_FILES, None) is None
    assert m._detect_env_move(VALUE_FILES, {}) is None


def test_detect_env_move_none_for_unrelated_rename():
    other = {"gcp/dev/x/customer.yaml": "gcp/dev/y/customer.yaml"}
    assert m._detect_env_move(VALUE_FILES, other) is None


def test_detect_env_move_none_for_same_dir_file_rename():
    # A file renamed WITHIN the same directory is not a folder move.
    same_dir = {f"{OLD_DIR}/customer.yaml": f"{OLD_DIR}/customer2.yaml"}
    assert m._detect_env_move(VALUE_FILES, same_dir) is None


# ── valueFiles rebase ───────────────────────────────────────────────────

def test_rebase_value_files_rebases_parent_refs():
    # THE core of the T2b fix: the relative tier/region refs must follow
    # the env folder to its new location so they resolve to the NEW tier's
    # defaults, exactly like the ApplicationSet will after merge.
    rebased = m._rebase_value_files(VALUE_FILES, OLD_DIR, NEW_DIR)
    assert f"$config/{NEW_DIR}/../config.yaml" in rebased          # -> monthly/config.yaml
    assert f"$config/{NEW_DIR}/../../config.yaml" in rebased       # -> ap1/config.yaml
    assert f"$config/{NEW_DIR}/customer.yaml" in rebased
    assert f"$config/{NEW_DIR}/cicd-versions.yaml" in rebased
    # Absolute shared defaults untouched.
    assert "$config/gcp/config.yaml" in rebased
    assert "$config/gcp/qa/config.yaml" in rebased
    # Nothing still references the old env dir.
    assert not any(OLD_DIR in vf for vf in rebased)
    assert len(rebased) == len(VALUE_FILES)


def test_rebase_value_files_name_only_rename_is_harmless():
    # A name-only env rename (Finding 6 case) maps <old>/../config.yaml to
    # <new>/../config.yaml which normalizes to the SAME tier file. The
    # rebase must be a safe no-op semantically.
    old = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-a"
    new = "gcp/dev/private-cloud/ap1/custom/pv-dev-06-b"
    import posixpath
    rebased = m._rebase_value_files([f"$config/{old}/../config.yaml"], old, new)
    assert posixpath.normpath(rebased[0].replace("$config/", "")) == \
        "gcp/dev/private-cloud/ap1/custom/config.yaml"


# ── effective chart version from ordered value files ────────────────────

def test_effective_chart_version_last_file_wins():
    ordered = ["$config/a.yaml", "$config/b.yaml", "$config/c.yaml"]
    vals = {
        "$config/a.yaml": "appspace:\n  version: 1.0.0\n",
        "$config/b.yaml": "appspace:\n  version: 2.0.0\n",
        "$config/c.yaml": "appspace:\n  other: x\n",
    }
    # helm -f semantics: later files override earlier ones; c has no
    # version so b's wins.
    assert m._effective_chart_version(ordered, vals) == "2.0.0"


def test_effective_chart_version_none_when_absent():
    ordered = ["$config/a.yaml"]
    vals = {"$config/a.yaml": "appspace:\n  other: x\n"}
    assert m._effective_chart_version(ordered, vals) is None


def test_effective_chart_version_skips_missing_files():
    ordered = ["$config/a.yaml", "$config/gone.yaml"]
    vals = {"$config/a.yaml": "appspace:\n  version: 3.1.4-dev\n"}
    assert m._effective_chart_version(ordered, vals) == "3.1.4-dev"


# ── downgrade detection ─────────────────────────────────────────────────

def test_is_version_downgrade_true_for_lower():
    assert m._is_version_downgrade("2602.2.11-dev", "2601.4.15-dev") is True
    assert m._is_version_downgrade("2603.0.0", "2602.9.9") is True
    assert m._is_version_downgrade("2602.4.9", "2602.4.8") is True


def test_is_version_downgrade_false_for_upgrade_equal_or_unparseable():
    assert m._is_version_downgrade("2602.4.8", "2602.4.9") is False
    assert m._is_version_downgrade("2602.4.9-dev", "2602.4.9-dev") is False
    assert m._is_version_downgrade("weird", "2602.4.9") is False
    assert m._is_version_downgrade("2602.4.9", None) is False
    assert m._is_version_downgrade(None, "2602.4.9") is False


# ── comment: big downgrade warning ──────────────────────────────────────

def _mk_diff_result(version_change=None):
    return m.DiffResult("===== x =====\n+a", [("apps/Deployment x", "+a")],
                        1, True, None, m.OUT_DIFF, "changes", version_change)


def test_format_comment_shouts_downgrade():
    body = m.format_comment(
        "deadbeef01234567",
        {"pv-qa-13-a-ms": _mk_diff_result(("2602.2.11-dev", "2601.4.15-dev"))})
    assert "DOWNGRADE" in body
    # Big letters: a top-level markdown heading line.
    assert any(l.startswith("# ") and "DOWNGRADE" in l for l in body.splitlines())
    assert "2602.2.11-dev" in body and "2601.4.15-dev" in body
    assert "pv-qa-13-a-ms" in body


def test_format_comment_no_downgrade_block_on_upgrade():
    body = m.format_comment(
        "deadbeef01234567",
        {"pv-qa-13-a-ms": _mk_diff_result(("2602.4.8", "2602.4.9"))})
    assert "DOWNGRADE" not in body


def test_format_comment_no_downgrade_block_when_no_version_change():
    body = m.format_comment("deadbeef01234567", {"a": _mk_diff_result(None)})
    assert "DOWNGRADE" not in body


def test_footer_status_mentions_downgrade():
    body = m.format_comment(
        "deadbeef01234567",
        {"pv-qa-13-a-ms": _mk_diff_result(("2602.2.11-dev", "2601.4.15-dev"))})
    footer = [l for l in body.splitlines() if l.startswith("**Status:**")][0]
    assert "DOWNGRADE" in footer.upper()


# ── latent issue: giant per-resource bodies must truncate WITH a marker ──

def test_format_app_diff_block_caps_giant_body_with_marker():
    giant = "\n".join(f"+line {i}" for i in range(4000))   # ~40k chars
    out = "\n".join(m._format_app_diff_block(
        "app-x", [("apps/Deployment big", giant)], giant, n_res=1, profile=_INLINE))
    assert len(out) < 15_000
    assert "truncated" in out.lower()


def test_format_app_diff_block_small_body_untouched():
    out = "\n".join(m._format_app_diff_block(
        "app-x", [("apps/Deployment small", "+one line")], "+one line", n_res=1, profile=_INLINE))
    assert "+one line" in out
    assert "truncated" not in out.lower()


# ── DiffResult backward compatibility ────────────────────────────────────

def test_diffresult_seven_positional_args_still_work():
    r = m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, "clean")
    assert r.version_change is None


# ── glue: full _run_one_diff flow for a tier move (T2b, PR #6666) ────────

def test_run_one_diff_tier_move_uses_new_tier_defaults_and_version(monkeypatch):
    # The exact live scenario: env moved custom/ -> monthly/, its own
    # customer.yaml no longer sets a version, monthly/config.yaml provides
    # the tier default. The PR-side render must (a) fetch the NEW location's
    # value chain incl. monthly/config.yaml, and (b) pull the PR chart at
    # the tier-default version, and (c) report the version change.
    app = "test-app-run-one-diff-tiermove"
    old_dir = "gcp/qa/private-cloud/ap1/custom/pv-t2b-a"
    new_dir = "gcp/qa/private-cloud/ap1/monthly/pv-t2b-a"
    value_files = [
        "$config/gcp/qa/config.yaml",
        f"$config/{old_dir}/../config.yaml",
        f"$config/{old_dir}/customer.yaml",
    ]

    monkeypatch.setitem(m._app_chart_map, app, "appspace-micro-services")
    monkeypatch.setitem(m._app_chart_revision_map, app, "2602.2.11-dev")
    monkeypatch.setitem(m._app_chart_registry_map, app, "helm-oci-dev.repo.appspace.com")
    monkeypatch.setitem(m._app_value_files_map, app, value_files)
    monkeypatch.setitem(m._app_namespace_map, app, "pv-t2b-a")

    pulled = []
    def fake_ensure_chart(registry, chart, ver):
        pulled.append(ver)
        return "/fake/chart/path"
    monkeypatch.setattr(m, "_ensure_chart", fake_ensure_chart)
    monkeypatch.setattr(m, "_helm_template",
        lambda chart_path, release, namespace, vals:
            ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", None))

    import posixpath
    def fake_bb_fetch_status(clean, sha):
        norm = posixpath.normpath(clean)
        if sha == "prsha111":
            if norm == "gcp/qa/config.yaml":
                return "appspace:\n  env: qa\n", m.BB_OK
            if norm == "gcp/qa/private-cloud/ap1/monthly/config.yaml":
                return "appspace:\n  version: 2601.4.15-dev\n", m.BB_OK
            if norm == f"{new_dir}/customer.yaml":
                return "appspace:\n  customerName: t2b\n", m.BB_OK   # NO version
            return None, m.BB_NOT_FOUND
        # main side: old paths, customer.yaml still has its own version
        if norm == "gcp/qa/config.yaml":
            return "appspace:\n  env: qa\n", m.BB_OK
        if norm == "gcp/qa/private-cloud/ap1/custom/config.yaml":
            return "# empty tier defaults\n", m.BB_OK
        if norm == f"{old_dir}/customer.yaml":
            return "appspace:\n  version: 2602.2.11-dev\n  customerName: t2b\n", m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()
    m._vf_inflight.clear()

    step = m._run_one_diff(
        app, pr_sha="prsha111", main_sha="mainsha111",
        changed_paths=[f"{old_dir}/customer.yaml", f"{new_dir}/customer.yaml"],
        renames={f"{old_dir}/customer.yaml": f"{new_dir}/customer.yaml"},
    )
    reason = step[1]
    assert reason is None, f"diff failed: {step[1]!r} {step[2]!r}"
    # (b) the PR chart was pulled at the tier-default version
    assert "2601.4.15-dev" in pulled, f"charts pulled: {pulled}"
    # (c) version change reported: current -> tier default
    assert step[3] == ("2602.2.11-dev", "2601.4.15-dev")


# ── v2.5.9: spurious rename pairing must not cross environment identities ─

# Live finding (PR #6673 on acme-config-dev, mirrors prod #3604 "renamed
# hsbc to -b for decom and rebuild"): a decommission+rebuild is done with
# genuinely different content (delete old customer.yaml, add new one under a
# different name) so Bitbucket correctly reports customer.yaml as a plain
# delete+add, NOT a rename. But cicd-versions.yaml is often boilerplate and
# byte-identical across unrelated environments, so Bitbucket's content-
# similarity rename detector PAIRS it with the new, unrelated environment's
# cicd-versions.yaml anyway. Both _detect_env_move and the older (v2.5.4)
# rename-following trusted that pairing on its own, wholesale-swapping the
# DELETED app's PR-side render for the UNRELATED new environment's identity:
# wrong "chart downgrade" attribution, and a nonsensical mixed-identity diff.

OLD_UNRELATED_DIR = "gcp/qa/private-cloud/ap1/custom/pv-qa-15-a"
NEW_UNRELATED_DIR  = "gcp/qa/private-cloud/ap1/custom/pv-qa-15-rebuild-a"
# Only the ancillary file is paired -- customer.yaml (the identity file) is
# NOT in renames at all, exactly like the live PR (it showed as delete+add).
SPURIOUS_RENAMES = {
    f"{OLD_UNRELATED_DIR}/cicd-versions.yaml": f"{NEW_UNRELATED_DIR}/cicd-versions.yaml",
}


def test_detect_env_move_ignores_ancillary_only_rename():
    value_files = [
        f"{OLD_UNRELATED_DIR}/customer.yaml",
        f"{OLD_UNRELATED_DIR}/cicd-versions.yaml",
    ]
    assert m._detect_env_move(value_files, SPURIOUS_RENAMES) is None, (
        "a rename of an ancillary file alone (no identity-file rename in the "
        "same direction) must NOT be treated as an environment move")


def test_detect_env_move_still_fires_when_identity_file_corroborates():
    # Regression guard: the real T2b move (customer.yaml itself renamed)
    # must keep working after tightening the check.
    move = m._detect_env_move(VALUE_FILES, RENAMES)
    assert move == (OLD_DIR, NEW_DIR)


def test_trusted_rename_dirs_requires_identity_file():
    trusted = m._trusted_rename_dirs(SPURIOUS_RENAMES)
    assert trusted == set()
    trusted2 = m._trusted_rename_dirs(RENAMES)
    assert (OLD_DIR, NEW_DIR) in trusted2


def test_pr_chart_revision_checked_does_not_follow_uncorroborated_rename(monkeypatch):
    # The general (non-move) version-bump detector must not read the new,
    # unrelated environment's version through the ancillary-file rename.
    app = "test-app-revcheck-spurious"
    monkeypatch.setitem(m._app_chart_revision_map, app, "2603.0.1-copr-30939-dev")

    def fake_bb_fetch_status(clean, sha):
        if clean == f"{NEW_UNRELATED_DIR}/cicd-versions.yaml":
            return "appspace:\n  version: 2602.4.9-dev\n", m.BB_OK  # unrelated env's version
        return None, m.BB_NOT_FOUND  # old path genuinely gone
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()

    new_rev, invalid = m._pr_chart_revision_checked(
        app, [f"{OLD_UNRELATED_DIR}/cicd-versions.yaml"], "prsha999",
        renames=SPURIOUS_RENAMES)
    assert new_rev is None, (
        f"must not adopt the unrelated environment's version, got {new_rev!r}")
    assert invalid is False


def test_pr_chart_revision_checked_still_follows_corroborated_rename(monkeypatch):
    # Regression guard: Finding 6's original case (real move, identity file
    # rename present) must keep working.
    app = "test-app-revcheck-corroborated"
    monkeypatch.setitem(m._app_chart_revision_map, app, "1.0.0")

    def fake_bb_fetch_status(clean, sha):
        if clean == f"{NEW_DIR}/cicd-versions.yaml":
            return "appspace:\n  version: 2.0.0\n", m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()

    new_rev, invalid = m._pr_chart_revision_checked(
        app, [f"{OLD_DIR}/cicd-versions.yaml"], "prsha998", renames=RENAMES)
    assert new_rev == "2.0.0"
    assert invalid is False


def test_run_one_diff_value_fetch_does_not_cross_unrelated_env(monkeypatch):
    # Full glue: the per-file value-fetch fallback (separate code path from
    # _detect_env_move) must also refuse the uncorroborated rename.
    app = "test-app-run-one-diff-spurious-rename"
    value_files = [f"{OLD_UNRELATED_DIR}/customer.yaml",
                   f"{OLD_UNRELATED_DIR}/cicd-versions.yaml"]

    monkeypatch.setitem(m._app_chart_map, app, "appspace-micro-services")
    monkeypatch.setitem(m._app_chart_revision_map, app, "2603.0.1-copr-30939-dev")
    monkeypatch.setitem(m._app_chart_registry_map, app, "helm-oci-dev.repo.appspace.com")
    monkeypatch.setitem(m._app_value_files_map, app, value_files)
    monkeypatch.setitem(m._app_namespace_map, app, "pv-qa-15-a")
    monkeypatch.setattr(m, "_ensure_chart", lambda registry, chart, ver: "/fake/chart/path")

    captured = {}
    def fake_helm_template(chart_path, release, namespace, vals):
        captured.setdefault("calls", []).append(dict(vals))
        return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", None
    monkeypatch.setattr(m, "_helm_template", fake_helm_template)

    def fake_bb_fetch_status(clean, sha):
        if sha == "prsha777":
            if clean == f"{NEW_UNRELATED_DIR}/cicd-versions.yaml":
                return "appspace:\n  version: 2602.4.9-dev\n", m.BB_OK
            return None, m.BB_NOT_FOUND   # old customer.yaml/cicd-versions.yaml genuinely gone
        if clean == f"{OLD_UNRELATED_DIR}/customer.yaml":
            return "appspace:\n  version: 2603.0.1-copr-30939-dev\n  customerName: qa-15\n", m.BB_OK
        if clean == f"{OLD_UNRELATED_DIR}/cicd-versions.yaml":
            return "appspace: {}\n", m.BB_OK
        return None, m.BB_NOT_FOUND
    monkeypatch.setattr(m, "_bb_fetch_status", fake_bb_fetch_status)
    m._vf_cache.clear()
    m._vf_inflight.clear()

    m._run_one_diff(
        app, pr_sha="prsha777", main_sha="mainsha777",
        changed_paths=[f"{OLD_UNRELATED_DIR}/customer.yaml",
                       f"{OLD_UNRELATED_DIR}/cicd-versions.yaml"],
        renames=SPURIOUS_RENAMES,
    )
    all_content = "".join(str(c) for call in captured.get("calls", []) for c in call.values())
    assert "qa-15-rebuild" not in all_content and "2602.4.9-dev" not in all_content, (
        "the unrelated new environment's content leaked into the deleted "
        "app's PR-side render")
