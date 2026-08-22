"""Goldens for the comment shapes that describe irreversible changes.

The existing golden corpus covers the everyday shapes — an ordinary version
bump, a resource deletion, a VM field change — and left the loudest ones
uncovered. Until this file, nothing pinned the exact rendering of:

  * a whole environment being deleted (cascade, and the orphan variant)
  * a data purge being armed
  * decommission being armed on a live environment
  * the COPS-2660 VM-strip break
  * (COPS-2707) Phase 1 on its own, and a teardown flag that is misspelled
    and therefore arms nothing

Those are the comments a reviewer reads immediately before approving something
they cannot undo, and the only tests on them were substring checks — which stay
green if the warning moves to the bottom of the comment, loses the emoji that
makes it visible, or ends up under a verdict line that contradicts it.

Two things about how these are built.

**They are driven, not written.** Each scenario calls the real generator
(`_evaluate_env_decommissions`, `_summarize_appspace_state_changes`) and feeds
its output to the real `format_comment`. Hand-writing the panel lines would pin
the assembly and leave the generation — where the COPS-2668 defects lived —
unguarded.

**The caches have to be cleared between scenarios.** `_bb_fetch_cached` and
`_flat_yaml_cached` memoise on `(sha, path)` forever, and this file reuses one
PR_SHA/BASE_SHA pair, so without the reset the second decommission scenario
silently renders the first one's config and the golden pins a lie. The
alternative used elsewhere in the suite is a per-test sha suffix; clearing is
less error-prone because forgetting it fails loudly rather than quietly.
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
BASE_SHA = "0000111122223333444455556666777788889999"

ENV = "pv-foo-c"
ENV_DIR = "gcp/prod/private-cloud/na2-a/monthly/pv-foo-c"
IDENT = f"{ENV_DIR}/customer.yaml"
APPS = ["pv-foo-c-glb", "pv-foo-c-ms", "pv-foo-c-ss"]

# A realistic small environment: a workload, a service, and the Namespace that
# carries `helm.sh/resource-policy: keep` — the retention marker COPS-2668 took
# out of the checksum-noise list, and the reason a decommissioned namespace
# survives its own cascade.
MANIFEST = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: %(a)s-web\n"
    "  namespace: pv-foo-c\nspec: {}\n"
    "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: %(a)s-web\n"
    "  namespace: pv-foo-c\nspec: {}\n"
    "---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: pv-foo-c\n"
    "  annotations:\n    helm.sh/resource-policy: keep\nspec: {}\n"
)

LIVE = "appspace:\n  customerName: foo\n"
ARMED = LIVE + "  decommission: true\n"
PURGE = ARMED + "  decommissionPurgeData: true\n"
VM_BLOCK = ("  infra:\n    deployLinuxServicesK8s:\n      enabled: true\n"
            "      defaults:\n        allowDeletion: true\n"
            "      instances:\n        svc-a:\n          enabled: true\n")
# The same block before Phase 1 arms it: VMs declared, deletion not allowed.
VM_BLOCK_UNARMED = ("  infra:\n    deployLinuxServicesK8s:\n      enabled: true\n"
                    "      instances:\n        svc-a:\n          enabled: true\n")
# COPS-2707: acme-config-prod #4376, byte for byte. One `m`.
MISSPELLED = LIVE + "  decomission: true\n"


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    """Pin the non-deterministic inputs, and clear the fetch memoisation.

    The cache reset is not hygiene: without it the second scenario in this
    file renders the first one's YAML (see the module docstring).
    """
    monkeypatch.setattr(m, "_ts", lambda: FIXED_TS)
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-prod")
    with m._vf_cache_lock:
        m._vf_cache.clear()
    m._yaml_cache.clear()
    yield
    with m._vf_cache_lock:
        m._vf_cache.clear()
    m._yaml_cache.clear()


def _result(**kw):
    kw.setdefault("outcome", m.OUT_NO_DIFF)
    return m.DiffResult("", [], 0, False, None, kw["outcome"],
                        kw.get("reason"), None, None, None, None)


def _assert_golden(name: str, body: str):
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = os.path.join(GOLDEN_DIR, f"{name}.md")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w") as f:
            f.write(body)
        pytest.skip(f"golden rewritten: {name}")
    if not os.path.exists(path):
        pytest.fail(f"no golden for {name!r}. Review it, then commit with "
                    f"UPDATE_GOLDEN=1:\n\n{body}")
    with open(path) as f:
        expected = f.read()
    if body != expected:
        import difflib
        pytest.fail(
            f"the comment a reviewer would read changed for {name!r}. If this "
            f"is intended, say WHY in the PR description and regenerate with "
            f"UPDATE_GOLDEN=1.\n\n" + "\n".join(difflib.unified_diff(
                expected.splitlines(), body.splitlines(),
                fromfile=f"golden/{name}.md (committed)",
                tofile="produced now", lineterm="")))


# ── the environment-deletion panels ──────────────────────────────────────

def _decommission_body(monkeypatch, base_yaml):
    """Drive the real decommission evaluator, then the real formatter."""
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s, **kw: (None, m.BB_NOT_FOUND) if s == PR_SHA
                        else (base_yaml, m.BB_OK))
    monkeypatch.setattr(m, "_render_main_side_resources",
                        lambda app, sha: m._parse_manifest_resources(
                            MANIFEST % {"a": app}))
    # Without this the note shells out to `argocd app get` per app, 30s each.
    monkeypatch.setattr(m, "_cascade_finalizer_live", lambda apps: None)
    for a in APPS:
        monkeypatch.setitem(m._app_chart_revision_map, a, "2603.1.0")

    lines, envs = m._evaluate_env_decommissions(
        [{"env_name": ENV, "identity_file": IDENT, "apps": APPS,
          "env_dir": ENV_DIR}], PR_SHA, BASE_SHA)
    assert envs == [ENV], "the scenario must actually confirm the deletion"
    return m.format_comment(
        PR_SHA, {a: _result(outcome=m.OUT_DECOMMISSIONED) for a in APPS},
        base_sha=BASE_SHA, decommission_lines=lines)


def test_golden_env_decommission_cascade(monkeypatch):
    """Cascade armed, data NOT purged. The verdict must say the resources go
    and the data stays — COPS-2668 had it claiming a purge on this exact
    shape, because the sentence denying the purge names the purge flag."""
    _assert_golden("env_decommission_cascade",
                   _decommission_body(monkeypatch, ARMED))


def test_golden_env_decommission_purge(monkeypatch):
    """Both flags armed: the only state in which customer data is destroyed."""
    _assert_golden("env_decommission_purge",
                   _decommission_body(monkeypatch, PURGE))


def test_golden_env_decommission_orphan(monkeypatch):
    """No cascade: the Applications go and the workloads keep running. This is
    the shape that printed '(resource preview unavailable)' underneath its own
    complete inventory until COPS-2668."""
    _assert_golden("env_decommission_orphan",
                   _decommission_body(monkeypatch, LIVE))


# ── the armed-state panels ───────────────────────────────────────────────

def _state_body(monkeypatch, base_yaml, head_yaml, results=None):
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s, **kw: (head_yaml, m.BB_OK) if s == PR_SHA
                        else (base_yaml, m.BB_OK))
    lines = m._summarize_appspace_state_changes(
        [IDENT], PR_SHA, BASE_SHA, {IDENT: APPS})
    assert lines, "the scenario must actually produce a state panel"
    return m.format_comment(
        PR_SHA, results or {APPS[0]: _result()},
        base_sha=BASE_SHA, appspace_state_lines=lines)


def test_golden_decommission_armed(monkeypatch):
    """Arming on a LIVE environment: nothing is deleted by this PR, but the
    next folder removal becomes destructive. The panel is the only signal —
    arming touches no manifest, so the resource diff has nothing to say."""
    _assert_golden("decommission_armed",
                   _state_body(monkeypatch, LIVE, ARMED))


def test_golden_vm_strip_while_arming(monkeypatch):
    """COPS-2660: the arming PR also removes the VM block, so helm stops
    rendering the VM CRs while they still carry `deletion-policy: abandon`.
    The VM, its disk and its IP are orphaned in GCP rather than deleted.

    Also the shape whose warning paragraph ran 369 characters for an ordinary
    environment name until COPS-2668 split it — over the 350-char prose-wall
    threshold the corpus guard enforces.
    """
    _assert_golden("vm_strip_while_arming",
                   _state_body(monkeypatch, LIVE + VM_BLOCK, ARMED))


def test_golden_decommission_phase1(monkeypatch):
    """COPS-2707: the first PR of a teardown, arming `allowDeletion` alone.

    acme-config-prod #4378 shipped this shape with no phase table at all —
    the VM panel reported the deletion-policy flip and nothing said which of
    the three phases the reviewer was looking at. The golden pins that the
    table is present, that Phase 1 reads "this PR" rather than "done", and
    that the panel is explicit nothing is deleted by merging it.

    The verdict here reads Routine because this harness drives the state
    panel alone. On the real PR the VM panel rides along and blocks; the
    phase table is positional context and must not raise a second finding
    for the same event (the COPS-2616 contract, asserted in
    test_cops2707_phase1_and_flag_typos.py).
    """
    _assert_golden("decommission_phase1",
                   _state_body(monkeypatch, LIVE + VM_BLOCK_UNARMED,
                               LIVE + VM_BLOCK))


def test_golden_teardown_flag_misspelled(monkeypatch):
    """COPS-2707: `appspace.decomission: true`, one `m`, exactly as merged on
    acme-config-prod #4376.

    Nothing is armed and nothing renders differently, so before this the
    whole comment read "Routine — nothing dangerous detected". The golden
    exists to keep the verdict red: this is the shape where a green comment
    is itself the incident.
    """
    _assert_golden("teardown_flag_misspelled",
                   _state_body(monkeypatch, LIVE, MISSPELLED))
