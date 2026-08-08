"""Version-transition fold: keep the needle visible inside bump PRs.

Where this comes from: a census of the last 100 acme-config-prod PRs.
The dominant operation is a platform version bump to ONE environment,
rendering as 185-473 near-identical resource sections (real PRs 3884 and
3891) whose only changed lines are image tags, chart labels, checksums,
version env values and deploy timestamps. Inlining them all pushed those
comments to the 245KB hard cap, and buried the one real change in the
noise (a new KCC annotation on PR 3891 that no reviewer could see).

The contract under test:
  1. A section folds ONLY if every changed line is provably version
     noise. Anything unpaired or unknown makes it a needle, kept inline.
  2. Needles are prioritised so they survive storage caps.
  3. The full-diff page (budget disabled) never folds anything.
  4. An intra-app budget bounds the comment for every PR shape, and
     never cuts a risk section.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
FIXED_TS = "2026-01-01 00:00 UTC"
PR_SHA = "abc12345def67890abc12345def67890abc12345"

# COPS-2612: the comment stopped inlining hunks by default. Cases
# below that assert the INLINE shape (fence counts, hunk contents,
# the intra-app budget, the repeat rollup) exercise a path that is
# still live -- always on the page, and in the comment on rollback --
# so they name the surface instead of relying on the default.
INLINE = m.COMMENT_PROFILE.replace(inline_diffs=True)
BASE_SHA = "0000111122223333444455556666777788889999"
URL = "https://diffs.appspace.example/diff/acme-config-prod/42/abc12345"


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    monkeypatch.setattr(m, "_ts", lambda: FIXED_TS)
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-prod")


def _assert_golden(name: str, body: str):
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = os.path.join(GOLDEN_DIR, f"{name}.md")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w") as f:
            f.write(body)
        pytest.skip(f"golden rewritten: {name}")
    if not os.path.exists(path):
        pytest.fail(f"no golden for {name!r}. Review then commit with "
                    f"UPDATE_GOLDEN=1:\n\n{body}")
    expected = open(path).read()
    if body != expected:
        import difflib
        delta = "\n".join(difflib.unified_diff(
            expected.splitlines(), body.splitlines(),
            fromfile=f"golden/{name}.md (committed)", tofile="produced now",
            lineterm=""))
        pytest.fail(f"comment changed for {name!r}:\n\n{delta}")


# ── Section builders (shapes copied from the real prod corpus) ──────────

def _bump_section(i, old="2603.1.9", new="2603.1.10"):
    """Every changed line is version noise: chart label, checksum,
    image tag, version env value, deploy timestamp env value."""
    return (f"/apps/Deployment svc-{i:03d}",
            "--- \n+++ \n@@ -5,20 +5,20 @@\n"
            "   labels:\n"
            f"-    helm.sh/chart: appspace-ms-{old}\n"
            f"+    helm.sh/chart: appspace-ms-{new}\n"
            "   annotations:\n"
            "-    checksum/micro-versions-info.yaml: 67a9616016f2b8cc\n"
            "+    checksum/micro-versions-info.yaml: 89801d07a5be6329\n"
            " spec:\n   template:\n     spec:\n       containers:\n"
            f"-        image: registry.example/svc-{i:03d}:{old}\n"
            f"+        image: registry.example/svc-{i:03d}:{new}\n"
            "         env:\n"
            "         - name: APPSPACE_PLATFORM_VERSION\n"
            f"-          value: {old}\n"
            f"+          value: {new}\n"
            "         - name: APPSPACE_DEPLOY_TIMESTAMP\n"
            "-          value: \"2026-08-04T20:00:00Z\"\n"
            "+          value: \"2026-08-05T20:00:00Z\"\n")


def _needle_section(old="2603.1.9", new="2603.1.10"):
    """Carries the version transition AND one real change: the exact
    shape of the reconcile-interval annotation found on prod PR 3891."""
    return ("/apps/StatefulSet mongo",
            "--- \n+++ \n@@ -3,8 +3,9 @@\n"
            " metadata:\n"
            "   annotations:\n"
            "+    cnrm.cloud.google.com/reconcile-interval-in-seconds: \"3600\"\n"
            " spec:\n   template:\n     spec:\n       containers:\n"
            f"-        image: registry.example/mongo:{old}\n"
            f"+        image: registry.example/mongo:{new}\n")


def _varied_section(i):
    """Ordinary but NOT version-classifiable: a real config change."""
    pad = "".join(f"   pad-{i:03d}-{j:02d}: value\n" for j in range(20))
    return (f"/v1/ConfigMap cfg-{i:03d}",
            "--- \n+++ \n@@ -2,24 +2,24 @@\n data:\n"
            f"-  maxConnections: \"100\"\n"
            f"+  maxConnections: \"2{i:02d}\"\n" + pad)


TRUE_DELETION = (
    "--- \n+++ \n@@ -1,5 +0,0 @@\n"
    "-apiVersion: v1\n-kind: Service\n-metadata:\n-  name: gone\n-spec: {}\n")


def _mk_result(secs):
    packed = m._package_sections(secs)
    (clean, stored, deleted, zeroed, fp, renamed, vm, fold) = packed
    return m.DiffResult(clean, stored, len(secs), True, None, m.OUT_DIFF,
                        "changes", None, deleted, zeroed, fp, renamed, vm,
                        fold)


# ── The classifier ───────────────────────────────────────────────────────

class TestClassifier:
    def test_all_noise_sections_fold(self):
        secs = [_bump_section(i) for i in range(5)]
        fold = m._classify_version_fold(secs)
        assert fold is not None
        assert fold["n_foldable"] == 5
        assert "2603.1.9" in fold["label"] and "2603.1.10" in fold["label"]
        assert set(fold["headers"]) == {h for h, _ in secs}

    def test_needle_never_folds(self):
        secs = [_bump_section(i) for i in range(4)] + [_needle_section()]
        fold = m._classify_version_fold(secs)
        assert fold["n_foldable"] == 4
        assert "/apps/StatefulSet mongo" not in fold["headers"]

    def test_below_minimum_no_fold(self):
        secs = [_bump_section(0), _bump_section(1)]
        assert m._classify_version_fold(secs) is None

    def test_pure_deletion_never_folds(self):
        secs = [_bump_section(i) for i in range(3)]
        secs.append(("/v1/Service gone", TRUE_DELETION))
        fold = m._classify_version_fold(secs)
        assert fold["n_foldable"] == 3
        assert "/v1/Service gone" not in fold["headers"]

    def test_exempt_headers_respected(self):
        secs = [_bump_section(i) for i in range(4)]
        fold = m._classify_version_fold(
            secs, exempt=frozenset({"/apps/Deployment svc-000"}))
        assert fold["n_foldable"] == 3
        assert "/apps/Deployment svc-000" not in fold["headers"]

    def test_unrelated_config_change_is_needle(self):
        secs = [_bump_section(i) for i in range(3)] + [_varied_section(0)]
        fold = m._classify_version_fold(secs)
        assert "/v1/ConfigMap cfg-000" not in fold["headers"]

    def test_env_value_pair_needs_a_known_transition(self):
        # a value: pair NOT matching any observed carrier is a needle
        stray = ("/apps/Deployment odd",
                 "--- \n+++ \n@@ -4,4 +4,4 @@\n         env:\n"
                 "         - name: MAX_WORKERS\n"
                 "-          value: 4\n"
                 "+          value: 16\n")
        secs = [_bump_section(i) for i in range(3)] + [stray]
        fold = m._classify_version_fold(secs)
        assert "/apps/Deployment odd" not in fold["headers"]


# ── Packaging: fold facts computed pre-cap, needles prioritised ─────────

class TestPackaging:
    def test_fold_travels_and_needle_prioritised(self):
        secs = [_bump_section(i) for i in range(6)] + [_needle_section()]
        packed = m._package_sections(secs)
        stored, fold = packed[1], packed[7]
        assert fold and fold["n_foldable"] == 6
        assert stored[0][0] == "/apps/StatefulSet mongo"

    def test_diffresult_legacy_construction_still_works(self):
        r = m.DiffResult("", [], 0, False, None, m.OUT_NO_DIFF, "clean")
        assert r.version_fold is None

    def test_no_fold_no_reorder(self):
        secs = [_varied_section(i) for i in range(4)]
        packed = m._package_sections(secs)
        assert [h for h, _ in packed[1]] == [h for h, _ in secs]
        assert packed[7] is None


# ── The comment ──────────────────────────────────────────────────────────

class TestComment:
    def test_fold_line_states_the_conclusion_and_names_the_needle(self):
        """COPS-2612 split this in two. The comment keeps every CONCLUSION
        the fold produces -- how many resources are version-only, which
        transition, which line classes, and which resource changed for
        another reason -- and stops carrying the hunk that proves it. The
        proof moved to the page; the sentence a reviewer decides on did
        not."""
        secs = [_bump_section(i) for i in range(6)] + [_needle_section()]
        body = m.format_comment(PR_SHA, {"pv-acme-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL)
        assert "**6 of 7 changed resource(s)**" in body
        assert "2603.1.9 \u2192 2603.1.10" in body
        assert "image tags" in body
        assert "Changed for another reason" in body, \
            "the reader must learn WHICH resource is not version-only"
        assert "svc-003" not in body
        assert body.count("```diff") == 0
        assert "7 resource(s) changed" in body
        assert "[base:" in body

    def test_the_needle_hunk_renders_on_the_inline_surface(self):
        secs = [_bump_section(i) for i in range(6)] + [_needle_section()]
        body = m.format_comment(PR_SHA, {"pv-acme-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE)
        assert "reconcile-interval-in-seconds" in body
        assert "svc-003" not in body
        assert body.count("```diff") == 1

    def test_full_page_never_folds(self):
        secs = [_bump_section(i) for i in range(6)] + [_needle_section()]
        body = m.format_comment(PR_SHA, {"pv-acme-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                readable_budget=0)
        assert "svc-003" in body
        assert "changed resource(s)** are" not in body
        assert body.count("```diff") == 7

    def test_intra_app_budget_folds_tail_sections(self):
        secs = [_varied_section(i) for i in range(40)]
        body = m.format_comment(PR_SHA, {"pv-big-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE.replace(readable_budget=8000))
        assert "cfg-000" in body
        assert "cfg-039" not in body
        assert "more changed resource(s) omitted" in body
        assert "[base:" in body

    def test_intra_app_budget_never_cuts_risk_sections(self):
        secs = [_varied_section(i) for i in range(40)]
        for k in range(6):
            secs.append((f"/v1/Service gone-{k}", TRUE_DELETION.replace(
                "name: gone", f"name: gone-{k}")))
        body = m.format_comment(PR_SHA, {"pv-big-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE.replace(readable_budget=6000))
        for k in range(6):
            assert f"gone-{k}" in body

    def test_fold_disabled_below_minimum(self):
        secs = [_bump_section(0), _needle_section()]
        body = m.format_comment(PR_SHA, {"pv-acme-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL)
        assert "changed resource(s)** are" not in body
        inline = m.format_comment(PR_SHA, {"pv-acme-a-ms": _mk_result(secs)},
                                  base_sha=BASE_SHA, artifact_url=URL,
                                  profile=INLINE)
        assert inline.count("```diff") == 2


# ── Goldens: freeze what the reviewer reads ──────────────────────────────

class TestGoldens:
    def test_platform_bump_single_env(self):
        secs = [_bump_section(i) for i in range(8)]
        body = m.format_comment(PR_SHA, {"pv-hp-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL)
        _assert_golden("platform_bump_single_env", body)

    def test_platform_bump_with_needle(self):
        secs = [_bump_section(i) for i in range(6)] + [_needle_section()]
        body = m.format_comment(PR_SHA, {"pv-hp-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL)
        _assert_golden("platform_bump_with_needle", body)

    def test_intra_app_budget(self):
        secs = [_varied_section(i) for i in range(12)]
        body = m.format_comment(PR_SHA, {"pv-big-a-ms": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE.replace(readable_budget=2500))
        _assert_golden("intra_app_budget", body)


# ── Repeated-change rollup ───────────────────────────────────────────────
# Second half of the same census finding. On prod PR 3891 the 384 sections
# that are NOT version noise collapse to 13 distinct changes, and two of
# them cover 364 sections: the same one-line KCC annotation added to every
# resource. Printing that hunk 364 times is not review material.

def _annotation_section(i):
    return (f"/iam.cnrm.cloud.google.com/IAMPolicyMember member-{i:03d}",
            "--- \n+++ \n@@ -2,6 +2,7 @@\n metadata:\n"
            "   annotations:\n"
            "+    cnrm.cloud.google.com/reconcile-interval-in-seconds: \"3600\"\n"
            "   labels:\n     app: appspace\n")


class TestRepeatRollup:
    """COPS-2612 scopes this class to the INLINE surface.

    Unlike the version fold, whose summary line states WHY resources
    changed and therefore stays in the comment, collapsing byte-identical
    sections is a display optimisation for hunks: its whole job is to stop
    printing the same hunk 364 times. With the hunks on the page, it has
    nothing to optimise in the comment, and "4 more resource(s) change
    exactly the same lines" has no antecedent when no representative is
    shown. The mechanism is still what the comment renders on rollback, so
    it keeps being tested here against an explicit inline profile.
    """
    def test_identical_change_renders_once(self):
        secs = [_annotation_section(i) for i in range(6)]
        secs.append(_varied_section(0))
        body = m.format_comment(PR_SHA, {"pv-acme-a-glb": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE)
        assert body.count("```diff") == 2
        assert "5 more resource(s)" in body
        assert "member-000" in body      # the representative hunk
        assert "member-001" in body      # named, so a reviewer can look
        assert "maxConnections" in body  # the odd one out still renders

    def test_below_minimum_all_render(self):
        secs = [_annotation_section(i) for i in range(2)]
        body = m.format_comment(PR_SHA, {"pv-acme-a-glb": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE)
        assert body.count("```diff") == 2
        assert "more resource(s)" not in body

    def test_risk_sections_are_never_grouped(self):
        secs = [(f"/v1/Service gone-{k}",
                 TRUE_DELETION.replace("name: gone", f"name: gone-{k}"))
                for k in range(4)]
        body = m.format_comment(PR_SHA, {"pv-acme-a-glb": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE)
        assert body.count("```diff") == 4
        for k in range(4):
            assert f"gone-{k}" in body

    def test_full_page_never_groups(self):
        secs = [_annotation_section(i) for i in range(6)]
        body = m.format_comment(PR_SHA, {"pv-acme-a-glb": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                readable_budget=0)
        assert body.count("```diff") == 6
        assert "more resource(s) change" not in body

    def test_rollup_and_fold_coexist(self):
        secs = ([_bump_section(i) for i in range(4)]
                + [_annotation_section(i) for i in range(5)])
        body = m.format_comment(PR_SHA, {"pv-acme-a-glb": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE)
        assert "changed resource(s)** are the version transition" in body
        assert "4 more resource(s)" in body
        assert body.count("```diff") == 1

    def test_golden_repeated_annotation(self):
        secs = [_annotation_section(i) for i in range(12)]
        body = m.format_comment(PR_SHA, {"pv-acme-a-glb": _mk_result(secs)},
                                base_sha=BASE_SHA, artifact_url=URL,
                                profile=INLINE)
        _assert_golden("repeated_annotation", body)
