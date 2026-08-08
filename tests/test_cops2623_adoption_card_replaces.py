"""The adoption card must replace the bullets it duplicates (COPS-2623).

`_kcc_adoption_card`'s own docstring states its contract:

    One card per adopted environment, replacing the nine "appears for the
    first time" bullets. Those are true of the ArgoCD objects and
    misleading about GCP, where the VM already exists and is adopted by
    resourceID.

`_summarize_vm_changes` does not honour it. It returns the card AND the
bullets, on both the routine and the danger path.

Measured on live single-environment adoption PRs rendered by 2.35.0:

| PR    | env              | bytes | rendered bullets | restatement |
|-------|------------------|-------|------------------|-------------|
| #4004 | pv-myschroders-a | 3,727 | 9                | 60%         |
| #4006 | pv-hsbc-c        | 3,599 | 9                | 59%         |
| #4015 | pv-sainsburys-a  | 3,693 | 9                | 59%         |

Target: under 1.5 KB.

There are TWO sources of routine lines, and the ticket's evidence shows
both are duplicated, so both have to be suppressed for an adopted
environment:

1. values-level keys, from the `for k in keys` loop (the
   `**linux VM (KCC) · svc**: added `svc.machineType` = ...` bullets);
2. rendered-manifest facts, from the `app_results` loop (the
   `ComputeInstance ...: new ... appears in this environment for the first
   time` bullets).

The line that must never be suppressed, for any environment, is a
dangerous one. Suppressing evidence is the point of the ticket;
suppressing a warning would be a different and much worse change.
"""
import os
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m

FIRST_TIME = "appears in this environment for the first time"


def _adoption(env="pv-hsbc-c"):
    """The shape _detect_kcc_adoption returns for a real adoption."""
    return {"kind": "adoption",
            "roles": [{"role": "svc", "instance_name": f"{env[:-2]}-svc-a",
                       "machine_type": "n2d-highmem-2",
                       "data_disk_gb": 128, "manage_metadata": False}]}


# --- 1. the card replaces, it does not accompany --------------------------

def test_an_adopted_environment_prints_the_card_and_not_its_routine_lines():
    lines = m._vm_panel_lines(
        adoption_cards=["\U0001f5a5\ufe0f VM INFRASTRUCTURE \u2014 ADOPTION"],
        adopted_envs={"pv-hsbc-c"},
        routine=[("pv-hsbc-c", "- `pv-hsbc-c` \u00b7 **linux VM (KCC) \u00b7 svc**: "
                               "added `svc.machineType` = `n2d-highmem-2`"),
                 ("pv-hsbc-c", f"- `pv-hsbc-c` \u00b7 `ComputeInstance x`: "
                               f"new ComputeInstance \u2014 {FIRST_TIME}")],
        dangerous=[])
    body = "\n".join(lines)
    assert "ADOPTION" in body
    assert FIRST_TIME not in body
    assert "svc.machineType" not in body


def test_another_environment_in_the_same_pr_keeps_all_of_its_bullets():
    lines = m._vm_panel_lines(
        adoption_cards=["ADOPTION CARD"],
        adopted_envs={"pv-hsbc-c"},
        routine=[("pv-hsbc-c", "- adopted env line"),
                 ("pv-other-a", "- `pv-other-a` \u00b7 **linux VM**: "
                                "ordinary change")],
        dangerous=[])
    body = "\n".join(lines)
    assert "pv-other-a" in body and "ordinary change" in body
    assert "adopted env line" not in body


# --- 2. a warning is never suppressed -------------------------------------

@pytest.mark.parametrize("warning", [
    "- \U0001f6a8 `pv-hsbc-c` \u00b7 **allowDeletion**: armed",
    "- \U0001f6a8 `pv-hsbc-c` \u00b7 **linux VM (KCC \u2190 legacy)**: data disk "
    "shrinks 256Gi \u2192 128Gi",
    "- \U0001f6a8 `pv-hsbc-c` \u00b7 **machineType**: n2d-standard-2 \u2192 -4",
    "- \U0001f6a8 `pv-hsbc-c` \u00b7 **zone**: us-central1-a \u2192 -b",
    "- \U0001f6a8 `pv-hsbc-c` \u00b7 `ComputeInstance x`: `deviceName` renamed",
])
def test_a_dangerous_line_survives_on_an_adopted_environment(warning):
    """The card suppresses evidence, never a verdict. Each of these rules
    can fire on a file that is otherwise a clean adoption."""
    lines = m._vm_panel_lines(
        adoption_cards=["ADOPTION CARD"],
        adopted_envs={"pv-hsbc-c"},
        routine=[("pv-hsbc-c", "- routine noise")],
        dangerous=[warning])
    body = "\n".join(lines)
    assert warning in body
    assert "ADOPTION CARD" in body, "the card still renders above the warning"
    assert "routine noise" not in body


# --- 3. repeated same-kind bullets collapse, adopted or not ---------------

def test_six_identical_snapshot_attachments_collapse_to_one_counted_line():
    """They differ only by daily/hourly/weekly x boot/data. That is one
    fact: the existing snapshot schedule comes under KCC management."""
    reps = [("pv-x-c", f"- `pv-x-c` \u00b7 `ComputeDiskResourcePolicyAttachment "
                       f"pv-x-svc-a-{d}-{p}`: new "
                       f"ComputeDiskResourcePolicyAttachment \u2014 {FIRST_TIME}")
            for d in ("data", "boot") for p in ("daily", "hourly", "weekly")]
    lines = m._vm_panel_lines(adoption_cards=[], adopted_envs=set(),
                              routine=reps, dangerous=[])
    body = "\n".join(lines)
    assert body.count(FIRST_TIME) == 1, "six lines must collapse to one"
    assert "6" in body and "ComputeDiskResourcePolicyAttachment" in body


def test_distinct_kinds_are_not_collapsed_together():
    reps = [("pv-x-c", f"- `pv-x-c` \u00b7 `ComputeInstance a`: new "
                       f"ComputeInstance \u2014 {FIRST_TIME}"),
            ("pv-x-c", f"- `pv-x-c` \u00b7 `ComputeDisk b`: new ComputeDisk "
                       f"\u2014 {FIRST_TIME}")]
    lines = m._vm_panel_lines(adoption_cards=[], adopted_envs=set(),
                              routine=reps, dangerous=[])
    body = "\n".join(lines)
    assert "ComputeInstance" in body and "ComputeDisk" in body


# --- 4. the fact the card is missing --------------------------------------

def test_the_card_states_that_deviceName_is_deliberately_not_rendered():
    """The single most dangerous field in this migration (COPS-2592 shipped
    an incident where a rendered deviceName detached a live disk). The card
    is silent about it being deliberately absent, which is exactly the
    reassurance an adoption reviewer is looking for."""
    card = "\n".join(m._kcc_adoption_card("pv-hsbc-c", _adoption()))
    assert "deviceName" in card
    assert "not rendered" in card or "leaves" in card
