"""COPR-31756 — parent cohort version bump must not override a child pin.

Live finding: acme-config-prod PR #3859 bumped
gcp/prod/private-cloud/na2-a/accelerated/config.yaml from 2603.1.7 to
2603.1.8. Diff Preview flagged pv-pt-279981116-c-* as a CHART VERSION
DOWNGRADE (2603.2.0-...-dev -> 2603.1.8) even though that env's
customer.yaml still pins 2603.2.0-...-dev and the PR did not touch it.

Root cause: _pr_chart_revision_checked returned the changed parent's
appspace.version without re-resolving the full Helm valueFiles chain
(last-wins).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


APP = "pv-pt-279981116-c-ms"
PINNED = "2603.2.0-20260802004-ap-68289-dev"
PARENT_NEW = "2603.1.8"
PARENT = "gcp/prod/private-cloud/na2-a/accelerated/config.yaml"
CUSTOMER = "gcp/prod/private-cloud/na2-a/accelerated/pv-pt279981116-c/customer.yaml"
VALUE_FILES = [
    f"$config/{PARENT}",
    f"$config/{CUSTOMER}",
]


def _setup(monkeypatch, files):
    m._app_chart_revision_map[APP] = PINNED
    m._app_value_files_map[APP] = list(VALUE_FILES)
    m._vf_cache.clear()

    def fake_fetch(clean, sha, repo=None):
        # Accept both raw and $config-stripped paths.
        clean = clean.replace("$config/", "").lstrip("/")
        content = files.get(clean)
        if content is None:
            return None, m.BB_NOT_FOUND
        return content, m.BB_OK

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)


def test_parent_bump_does_not_override_customer_pin(monkeypatch):
    _setup(monkeypatch, {
        PARENT: f"appspace:\n  version: {PARENT_NEW}\n",
        CUSTOMER: f"appspace:\n  version: {PINNED}\n",
    })
    # Only the parent file changed in the PR, same as #3859.
    new_rev, invalid = m._pr_chart_revision_checked(APP, [PARENT], "prsha3859")
    assert invalid is False
    assert new_rev is None, (
        "child customer.yaml still pins the live revision; parent cohort bump "
        "must not be reported as a chart downgrade/bump"
    )


def test_parent_bump_applies_when_child_has_no_version(monkeypatch):
    _setup(monkeypatch, {
        PARENT: f"appspace:\n  version: {PARENT_NEW}\n",
        CUSTOMER: "appspace:\n  customerName: pt-279981116\n",
    })
    new_rev, invalid = m._pr_chart_revision_checked(APP, [PARENT], "prsha3859")
    assert (new_rev, invalid) == (PARENT_NEW, False)


def test_child_version_change_still_detected(monkeypatch):
    child_new = "2603.2.0-20260803001-dev"
    _setup(monkeypatch, {
        PARENT: f"appspace:\n  version: {PARENT_NEW}\n",
        CUSTOMER: f"appspace:\n  version: {child_new}\n",
    })
    new_rev, invalid = m._pr_chart_revision_checked(APP, [CUSTOMER], "prsha-child")
    assert (new_rev, invalid) == (child_new, False)


def test_fallback_without_value_files_map_keeps_old_behavior(monkeypatch):
    # Unit-test / early-cache path: no live valueFiles yet -> first changed
    # file with a different version still wins (pre-COPR-31756 behavior).
    m._app_chart_revision_map[APP] = PINNED
    m._app_value_files_map.pop(APP, None)
    m._vf_cache.clear()
    monkeypatch.setattr(
        m, "_bb_fetch_status",
        lambda clean, sha, repo=None: (
            f"appspace:\n  version: {PARENT_NEW}\n", m.BB_OK))
    new_rev, invalid = m._pr_chart_revision_checked(APP, [PARENT], "prsha-fallback")
    assert (new_rev, invalid) == (PARENT_NEW, False)


def test_missing_customer_yaml_does_not_invent_parent_bump(monkeypatch):
    # If customer.yaml is in the live chain but unread (404 / flaky Bitbucket),
    # do NOT let the parent alone decide the bump — that reintroduces #3859.
    _setup(monkeypatch, {
        PARENT: f"appspace:\n  version: {PARENT_NEW}\n",
        # CUSTOMER intentionally absent from files dict -> BB_NOT_FOUND
    })
    new_rev, invalid = m._pr_chart_revision_checked(APP, [PARENT], "prsha-miss")
    assert invalid is False
    assert new_rev is None


def test_rename_with_value_files_map_still_detects_bump(monkeypatch):
    # Production always has valueFiles cached. A customer.yaml rename+bump
    # must still resolve via the chain (old path 404, rename fill-in).
    old_customer = CUSTOMER
    new_customer = "gcp/prod/private-cloud/na2-a/accelerated/pv-pt279981116-renamed-c/customer.yaml"
    child_new = "2603.2.0-20260803001-dev"
    m._app_chart_revision_map[APP] = PINNED
    m._app_value_files_map[APP] = [
        f"$config/{PARENT}",
        f"$config/{old_customer}",
    ]
    m._vf_cache.clear()

    def fake_fetch(clean, sha, repo=None):
        clean = str(clean).replace("$config/", "").lstrip("/")
        if clean == PARENT:
            return f"appspace:\n  version: {PARENT_NEW}\n", m.BB_OK
        if clean == new_customer:
            return (
                "appspace:\n"
                "  customerName: pt-279981116\n"
                "  suffix: c\n"
                f"  version: {child_new}\n",
                m.BB_OK,
            )
        if clean == old_customer and sha == "mainsha":
            return (
                "appspace:\n"
                "  customerName: pt-279981116\n"
                "  suffix: c\n"
                f"  version: {PINNED}\n",
                m.BB_OK,
            )
        return None, m.BB_NOT_FOUND

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(m, "_bb_fetch_cached",
                        lambda path, sha, repo=None: fake_fetch(path, sha, repo=repo))

    new_rev, invalid = m._pr_chart_revision_checked(
        APP, [old_customer], "prsha-rename",
        main_sha="mainsha",
        renames={old_customer: new_customer},
    )
    assert invalid is False
    assert new_rev == child_new
