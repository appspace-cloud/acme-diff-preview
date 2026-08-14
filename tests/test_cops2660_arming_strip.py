"""COPS-2660: arming decommission must not reassure when the same PR breaks it.

acme-config-prod PR #4247 (pv-fordpoc-a) armed decommission like this in one
change: added `defaults.allowDeletion: true`, added `appspace.decommission:
true` (+ purge) — and REMOVED `deployLinuxServicesK8s.enabled` and the whole
`svc` block.

The comment for that shape said Phase 1 was ✅ done, because the check only
looks for `allowDeletion` in the new file. But stripping the role blocks means
helm stops rendering the VM CRs the moment the PR merges, so ArgoCD prunes
them while the live objects still carry `deletion-policy: abandon` — the real
GCP VM, its disk and its IP are ORPHANED, not deleted. The panel reassured
from git flags without verifying the arming path can actually take effect,
the same class of failure as COPS-2656.

Live check on the spoke before merge confirmed it: pv-fordpoc-svc-a still had
`abandon` and `deletionProtection=true`.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import comment_render  # noqa: E402
import diff_preview as m  # noqa: E402
import vm_analysis  # noqa: E402

IDENT = "gcp/prod/private-cloud/na3-a/accelerated/pv-fordpoc-a/customer.yaml"
APPS = ["pv-fordpoc-a-ss", "pv-fordpoc-a-ms"]
PATH_MAP = {IDENT: APPS}

# The #4247 before-shape: a live VM declared and running, nothing armed.
OLD_YAML = """appspace:
  customerName: fordpoc
  infra:
    deployLinuxServicesK8s:
      enabled: true
      svc:
        enabled: true
        instances:
          - pv-fordpoc-svc-a
"""

# The #4247 after-shape: armed for teardown, VM block stripped in the same diff.
STRIPPED_YAML = """appspace:
  customerName: fordpoc
  decommission: true
  decommissionPurgeData: true
  infra:
    deployLinuxServicesK8s:
      defaults:
        allowDeletion: true
"""

# The runbook-correct after-shape: armed, VM block kept intact.
HAPPY_YAML = """appspace:
  customerName: fordpoc
  decommission: true
  infra:
    deployLinuxServicesK8s:
      enabled: true
      defaults:
        allowDeletion: true
      svc:
        enabled: true
        instances:
          - pv-fordpoc-svc-a
"""


def _mk_fetch(files_by_sha):
    def fake(path, sha, repo=None):
        v = files_by_sha.get((path, sha))
        return (v, m.BB_OK) if v is not None else (None, m.BB_NOT_FOUND)
    return fake


def _panel(monkeypatch, old_yaml, new_yaml, base="base2660", pr="pr2660"):
    monkeypatch.setattr(m, "_bb_fetch_status", _mk_fetch({
        (IDENT, base): old_yaml,
        (IDENT, pr): new_yaml,
    }))
    return "\n".join(
        m._summarize_appspace_state_changes([IDENT], pr, base, PATH_MAP))


# ── the detector itself ─────────────────────────────────────────────────────

def test_the_detector_names_the_stripped_keys():
    old = m._flatten_yaml(m._yaml_safe_load(OLD_YAML))
    new = m._flatten_yaml(m._yaml_safe_load(STRIPPED_YAML))
    stripped = vm_analysis._vm_config_stripped(old, new)
    assert stripped, "the #4247 shape must be detected"
    assert any(k.endswith("deployLinuxServicesK8s.enabled") for k in stripped)
    assert any(".svc." in k for k in stripped), \
        "the removed role block must be named, not just the top-level flag"


def test_flipping_enabled_to_false_counts_as_stripping():
    """`enabled: false` stops the render exactly like deleting the key."""
    old = m._flatten_yaml(m._yaml_safe_load(OLD_YAML))
    flipped = OLD_YAML.replace("enabled: true", "enabled: false", 1)
    new_y = flipped + "  decommission: true\n"
    new = m._flatten_yaml(m._yaml_safe_load(new_y))
    new["appspace.infra.deployLinuxServicesK8s.defaults.allowDeletion"] = "true"
    assert vm_analysis._vm_config_stripped(old, new)


def test_pure_arming_strips_nothing():
    old = m._flatten_yaml(m._yaml_safe_load(OLD_YAML))
    new = m._flatten_yaml(m._yaml_safe_load(HAPPY_YAML))
    assert vm_analysis._vm_config_stripped(old, new) == []


# ── the panel ───────────────────────────────────────────────────────────────

def test_the_4247_shape_breaks_phase_1_and_warns(monkeypatch):
    out = _panel(monkeypatch, OLD_YAML, STRIPPED_YAML)
    assert "DECOMMISSION ARMED" in out, "the arming banner itself must stay"
    assert comment_render._DECOM_VM_STRIP_HDR in out
    # Phase 1 must not reassure. The literal done mark must be absent from
    # its row, and the broken state present.
    phase1 = next(l for l in out.splitlines() if "Phase 1" in l)
    assert "✅" not in phase1, f"Phase 1 must not read done: {phase1}"
    assert "broken" in phase1.lower(), phase1
    # The warning must teach the fix, in the runbook's terms.
    assert "orphan" in out.lower()
    assert "allowDeletion" in out
    assert "keep" in out.lower()
    # And name what was stripped, so the reviewer can judge.
    assert "deployLinuxServicesK8s.enabled" in out


def test_happy_path_arming_still_reads_done_and_does_not_warn(monkeypatch):
    out = _panel(monkeypatch, OLD_YAML, HAPPY_YAML)
    assert "DECOMMISSION ARMED" in out
    assert comment_render._DECOM_VM_STRIP_HDR not in out
    phase1 = next(l for l in out.splitlines() if "Phase 1" in l)
    assert "✅" in phase1 and "done" in phase1, phase1


def test_stripping_an_already_armed_environment_warns_standalone(monkeypatch):
    """Arming happened in an earlier PR; this one only strips the block.

    No arming transition fires in this diff, so without a standalone check
    the panel would say nothing at all -- on the PR that actually breaks
    the teardown.
    """
    out = _panel(monkeypatch, HAPPY_YAML, STRIPPED_YAML)
    assert comment_render._DECOM_VM_STRIP_HDR in out
    assert "orphan" in out.lower()


def test_stripping_an_unarmed_environment_is_not_this_warning(monkeypatch):
    """No teardown intent anywhere: removing VM config is the ordinary
    dangerous-deletion territory other panels own, not an arming break."""
    new_y = "appspace:\n  customerName: fordpoc\n"
    out = _panel(monkeypatch, OLD_YAML, new_y)
    assert comment_render._DECOM_VM_STRIP_HDR not in out


# ── the merge summary ───────────────────────────────────────────────────────

def _summary(appspace_state_lines):
    return "\n".join(comment_render._build_merge_summary(
        {}, {}, [], [], appspace_state_lines, [], False))


def test_the_summary_blocks_and_names_the_orphaning():
    fake_panel = [comment_render._DECOM_VM_STRIP_HDR, "",
                  "## \U0001f512⚠️ DECOMMISSION ARMED for `pv-fordpoc-a` ⚠️\U0001f512"]
    out = _summary(fake_panel)
    assert "DO NOT MERGE" in out
    assert "orphan" in out.lower()
    assert "allowDeletion" in out


def test_plain_arming_summary_is_unchanged():
    fake_panel = ["## \U0001f512⚠️ DECOMMISSION ARMED for `pv-x` ⚠️\U0001f512"]
    out = _summary(fake_panel)
    assert "DO NOT MERGE" in out, "arming already blocks, and must keep doing so"
    assert "orphan" not in out.lower(), \
        "the orphaning finding must only fire on the broken shape"
