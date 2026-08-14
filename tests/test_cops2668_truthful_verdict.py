"""The verdict must never be greener than the evidence (COPS-2668, P0).

The 2.77.0 audit found the service's failure mode is not a crash: it is a
confident false statement about a dangerous change. Three of those live here.

1. `[blocked]` resolved to SUCCESSFUL. `_extract_status_token` recognised
   clean|permanent|transient, but process_pr also emits `[blocked]` -- for an
   empty `microservices.definitions`, which breaks image names across a whole
   environment on merge (COPR-31637, ImagePullBackOff). An unrecognised token
   fell through every branch of fix_stuck_inprogress to its final
   `else: SUCCESSFUL, "No manifest changes"`. So the service detected the
   danger, blocked it correctly, and then a pod killed mid-flight resolved the
   merge gate to green. The blocking comment stayed on the PR saying the
   opposite of the status next to it.

2. A whole VM section's facts discarded by a `continue`. In the disk-shrink
   check, `except ValueError: continue` targets the OUTER `for header, body in
   sections` loop -- there is no loop between them -- so one unparseable size
   drops the section's untracked-keys note AND its `facts.append`. The comment
   above the `continue` says "The field is still reported above as an ordinary
   changed field", which is exactly what does not happen. A templated or
   unit-suffixed size is enough to make the panel silent about a disk it can
   see changing.

3. `helm.sh/resource-policy` classified as checksum noise.
   `_is_checksum_only_section` matches by bare substring, and the annotation
   sits in `_ANNOTATION_NOISE` next to genuinely cosmetic entries like
   `checksum/` and `tracking-id`. But that annotation is what keeps a resource
   alive through an Argo cascade: `keep` -> absent is the difference between a
   Namespace surviving a decommission and being deleted with everything in it.
   The filter runs upstream of every safety detector, so the change is not
   merely unreported -- it is invisible to the code that decides whether the
   PR is dangerous.

Each test below fails on the pre-fix code.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m
import app_meta
import manifest
import vm_analysis


# ── 1. the [blocked] token ────────────────────────────────────────────────

def _blocked_comment():
    """A comment shaped exactly like the one process_pr emits for an empty
    `microservices.definitions` (diff_preview.py, the COPR-31637 guard)."""
    return (
        "## \U0001f52d Diff Preview\n\n"
        "**Commit** `deadbeef`\n\n"
        "⛔ **Blocked** — empty `microservices.definitions` would "
        "break image names on merge\n\n"
        "---\n**Status:** ⛔ Blocked — empty "
        "`microservices.definitions` would break image names on merge\n"
        f"*2026-01-01 00:00 — {m.COMMENT_MARKER} [blocked]*"
    )


def test_blocked_token_is_recognised():
    """The reader must not silently return "" for a token the writer emits."""
    assert app_meta._extract_status_token(_blocked_comment()) == "blocked"


def test_stuck_inprogress_with_blocked_comment_resolves_failed(monkeypatch):
    """The bug: a killed pod turned a correctly-blocked PR into a green gate."""
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    captured = {}
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, desc, pr_id=None: captured.update(state=state))

    m.fix_stuck_inprogress("deadbeef01234567", 999, _blocked_comment())
    assert captured["state"] == "FAILED", (
        "a blocked comment must never resolve the build status to SUCCESSFUL")


def test_stuck_inprogress_legacy_blocked_text_without_token_resolves_failed(monkeypatch):
    """Belt and braces: a blocked comment posted before the token existed is
    still recognisable by its status line, and must also stay red."""
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    captured = {}
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, desc, pr_id=None: captured.update(state=state))

    legacy = ("## Diff Preview\n\n**Commit** `deadbeef`\n\n"
              "---\n**Status:** ⛔ Blocked — empty "
              "`microservices.definitions` would break image names on merge\n")
    m.fix_stuck_inprogress("deadbeef01234567", 999, legacy)
    assert captured["state"] == "FAILED"


def test_clean_comment_still_resolves_successful(monkeypatch):
    """The fix must not turn genuinely clean comments red."""
    monkeypatch.setattr(m, "http", lambda *a, **k: {"state": "INPROGRESS"})
    captured = {}
    monkeypatch.setattr(m, "post_build_status",
                        lambda sha, state, desc, pr_id=None: captured.update(state=state))

    clean = ("## Diff Preview\n\n**Commit** `deadbeef`\n\n"
             "✅ No manifest changes\n"
             f"*2026-01-01 00:00 — {m.COMMENT_MARKER} [clean]*")
    m.fix_stuck_inprogress("deadbeef01234567", 999, clean)
    assert captured["state"] == "SUCCESSFUL"


# ── 2. the VM section swallowed by `continue` ─────────────────────────────

DISK = ("/compute.cnrm.cloud.google.com/ComputeDisk "
        "pv-euroclear-a/pv-euroclear-svc-a-data")

# A size that int(float(...)) cannot parse -- a unit suffix is the ordinary
# case, and a templated value behaves identically.
UNPARSEABLE_SIZE_WITH_REAL_CHANGE = """     location: europe-west1-d
-    size: 100Gi
+    size: 200Gi
-    type: pd-ssd
+    type: pd-balanced
"""


def test_unparseable_size_does_not_discard_the_section():
    """One size the shrink check cannot compare must not silence the section.

    `type: pd-ssd -> pd-balanced` is an immutable-field change the panel calls
    destroy-and-recreate. Pre-fix, the ValueError on `100Gi` skipped straight
    to the next section and this never reached the operator.
    """
    facts = vm_analysis._detect_vm_changes([(DISK, UNPARSEABLE_SIZE_WITH_REAL_CHANGE)])
    assert facts, "the section must still produce a fact"
    f = facts[0]
    blob = " ".join(f["dangerous"]) + " ".join(str(x) for x in f["fields"])
    assert "immutable" in blob or "type" in blob, (
        "the disk type change must survive an unparseable size")


def test_unparseable_size_still_reports_the_size_field():
    """The code comment promises the field is "still reported above as an
    ordinary changed field". Hold it to that."""
    f = vm_analysis._detect_vm_changes(
        [(DISK, UNPARSEABLE_SIZE_WITH_REAL_CHANGE)])[0]
    assert any("size" in str(x) for x in f["fields"])


def test_numeric_shrink_is_still_dangerous():
    """The shrink detector itself must keep working."""
    shrink = """     location: europe-west1-d
-    size: 200
+    size: 100
"""
    f = vm_analysis._detect_vm_changes([(DISK, shrink)])[0]
    assert any("DECREASES" in d for d in f["dangerous"])


def test_numeric_growth_is_not_dangerous():
    grow = """     location: europe-west1-d
-    size: 100
+    size: 200
"""
    f = vm_analysis._detect_vm_changes([(DISK, grow)])[0]
    assert not any("DECREASES" in d for d in f["dangerous"])


# ── 3. resource-policy is not noise ───────────────────────────────────────

def test_resource_policy_change_is_not_checksum_noise():
    """`keep` disappearing is what lets an Argo cascade delete the resource."""
    body = ("-    helm.sh/resource-policy: keep\n"
            "+    helm.sh/resource-policy: \n")
    assert not manifest._is_checksum_only_section(body), (
        "a cascade-retention change must never be filtered as checksum noise")


def test_resource_policy_removal_is_not_checksum_noise():
    body = "-    helm.sh/resource-policy: keep\n"
    assert not manifest._is_checksum_only_section(body)


def test_genuine_checksum_noise_is_still_filtered():
    """The filter must keep doing its job for real noise."""
    body = ("-        checksum/config: abc123\n"
            "+        checksum/config: def456\n"
            "-        argocd.argoproj.io/tracking-id: x\n"
            "+        argocd.argoproj.io/tracking-id: y\n")
    assert manifest._is_checksum_only_section(body)


def test_resource_policy_mixed_with_noise_is_not_filtered():
    """One real change among noise lines must defeat the filter."""
    body = ("-        checksum/config: abc123\n"
            "+        checksum/config: def456\n"
            "-    helm.sh/resource-policy: keep\n")
    assert not manifest._is_checksum_only_section(body)
