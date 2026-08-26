"""Advisory notes on VM uptime schedules (COPS-2714).

The feature lets customer.yaml park a linux-services VM on a daily
window. The chart refuses what it can be certain of and the merge is
blocked by the render, so these notes cover only what renders clean,
applies clean, and still does not mean what the author meant.

The half of this file that matters most is the LEGITIMATE TWINS. Every
suspicious shape here has a real configuration that looks identical --
a weekend park, a nightly bounce, an overnight batch runner -- and a
checker that flagged those would be wrong often enough to be ignored.
Each twin below is asserted silent, so a future tightening of the rules
has to break a named, explained test rather than quietly start nagging.

Nothing in this module can block a merge: it returns markdown, and the
caller appends it to the panel's routine lines. That is asserted too,
at the call site, in test_notes_never_reach_the_dangerous_list.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import pytest
import uptime_schedule as u

GOOD = {"stop": "0 21 * * *", "start": "0 7 * * *",
        "timeZone": "Europe/Madrid"}


def _text(notes):
    return "\n".join(notes).lower()


# --------------------------------------------------------------------
# The documented good case, and the legitimate twins.
# --------------------------------------------------------------------

def test_the_documented_example_says_nothing():
    assert u.schedule_notes("rabbit", GOOD) == []


@pytest.mark.parametrize("name,sched", [
    # Park over the weekend: stop Friday evening, start Monday morning.
    # Looks like mismatched day sets; it is the whole point of the feature.
    ("weekend park", {"stop": "0 21 * * 5", "start": "0 7 * * 1",
                      "timeZone": "Europe/Madrid"}),
    # Weeknights only, available to testers at the weekend.
    ("weeknights only", {"stop": "0 21 * * 1-5", "start": "0 7 * * 1-5",
                         "timeZone": "Europe/Madrid"}),
    # A Tue-Sat working week. Off-by-one day sets read as a typo and are
    # simply how retail and hospitality customers work.
    ("tue-sat week", {"stop": "0 21 * * 2-6", "start": "0 7 * * 2-6",
                      "timeZone": "Europe/Madrid"}),
    # Overnight batch runner: up at night, parked during the day. The
    # inverted look is the intent.
    ("overnight runner", {"stop": "0 7 * * *", "start": "0 21 * * *",
                          "timeZone": "Europe/Madrid"}),
    # Stop-only is documented and supported; the VM is parked and started
    # by hand when somebody needs it.
    ("stop only", {"stop": "0 21 * * *", "timeZone": "Europe/Madrid"}),
    # A zone that is valid, real, and looks odd. The chart's own shape
    # regex rejects this family; this module must not repeat that bug.
    ("fixed offset zone", {"stop": "0 21 * * *", "start": "0 7 * * *",
                           "timeZone": "Etc/GMT+12"}),
    # Spain spans two zones. Without geography this is unknowable, so it
    # must not be guessed at.
    ("canary islands", {"stop": "0 21 * * *", "start": "0 7 * * *",
                        "timeZone": "Atlantic/Canary"}),
    # A wide gap on the same calendar is an ordinary window, not a race.
    ("ordinary window", {"stop": "30 22 * * *", "start": "45 6 * * *",
                         "timeZone": "Europe/Madrid"}),
])
def test_legitimate_twins_stay_silent(name, sched):
    assert u.schedule_notes("rabbit", sched) == [], name


def test_a_zone_without_dst_is_not_flagged_for_the_two_oclock_hour():
    # The 02:00 note is about the DST discontinuity. A zone that never
    # shifts has none, so the same hour is unremarkable there.
    notes = u.schedule_notes("rabbit", {
        "stop": "30 2 * * *", "start": "0 7 * * *", "timeZone": "UTC"})
    assert notes == []


# --------------------------------------------------------------------
# The certain ones: wrong, and silent in GCP until apply time.
# --------------------------------------------------------------------

def test_a_misspelled_zone_is_named():
    # Europe/Madird has the right shape, so the chart renders it; only
    # the tz database knows it does not exist.
    notes = u.schedule_notes("rabbit", dict(GOOD, timeZone="Europe/Madird"))
    assert len(notes) == 1
    assert "europe/madird" in _text(notes)
    assert "degraded" in _text(notes)


@pytest.mark.parametrize("field,expr,word", [
    ("day-of-month", "0 21 32 * *", "day-of-month"),
    ("month", "0 21 * 13 *", "month"),
    ("day-of-week", "0 21 * * 8", "day-of-week"),
])
def test_out_of_range_calendar_fields_are_named(field, expr, word):
    notes = u.schedule_notes("rabbit", dict(GOOD, stop=expr))
    assert any(word in n.lower() for n in notes), field


def test_an_impossible_date_says_it_can_never_fire():
    notes = u.schedule_notes("rabbit", dict(GOOD, stop="0 21 31 4 *"))
    assert any("never fire" in n.lower() for n in notes)


def test_29_february_is_allowed_because_leap_years_exist():
    # A recurring rule that fires only in leap years is unusual, not
    # impossible -- the distinction 30 February does not have.
    notes = u.schedule_notes("rabbit", dict(GOOD, stop="0 21 29 2 *"))
    assert notes == []


# --------------------------------------------------------------------
# The suspicious ones. Each states what GCP does; none blocks.
# --------------------------------------------------------------------

def test_identical_stop_and_start_explains_the_silent_park():
    notes = u.schedule_notes("rabbit", dict(
        GOOD, stop="0 21 * * *", start="0 21 * * *"))
    assert len(notes) == 1
    t = _text(notes)
    assert "prioritises the stop" in t and "no error" in t


def test_a_gap_below_the_jitter_window_is_flagged_with_its_size():
    notes = u.schedule_notes("rabbit", dict(
        GOOD, stop="0 21 * * *", start="5 21 * * *"))
    assert len(notes) == 1
    assert "5 minutes apart" in _text(notes)
    # Worded as worth a second look, because the nightly bounce is real.
    assert "second look" in _text(notes)


def test_a_gap_at_the_jitter_boundary_is_not_flagged():
    # Exactly 15 minutes is the documented limit, not inside it. An
    # off-by-one here would nag at a schedule GCP handles.
    notes = u.schedule_notes("rabbit", dict(
        GOOD, stop="0 21 * * *", start="15 21 * * *"))
    assert notes == []


def test_the_gap_check_is_skipped_across_different_day_sets():
    # Friday 21:00 to Monday 07:00 is minutes apart on a clock and three
    # days apart in reality. Comparing them would invent a race.
    notes = u.schedule_notes("rabbit", {
        "stop": "0 21 * * 5", "start": "5 21 * * 1",
        "timeZone": "Europe/Madrid"})
    assert notes == []


def test_day_of_month_and_day_of_week_together_explains_the_union():
    notes = u.schedule_notes("rabbit", dict(GOOD, stop="0 21 15 * 5"))
    assert len(notes) == 1
    t = _text(notes)
    assert "union" in t and "not" in t


def test_the_two_oclock_hour_is_flagged_in_a_dst_zone():
    notes = u.schedule_notes("rabbit", dict(GOOD, stop="30 2 * * *"))
    assert len(notes) == 1
    assert "dst" in _text(notes)


def test_start_only_says_nothing_enforces_the_power_state():
    notes = u.schedule_notes("rabbit", {"start": "0 7 * * *",
                                        "timeZone": "Europe/Madrid"})
    assert len(notes) == 1
    assert "start-only" in _text(notes)


# --------------------------------------------------------------------
# Degrading safely.
# --------------------------------------------------------------------

def test_a_container_without_a_tz_database_stays_silent(monkeypatch):
    # Absence of the database is a fact about the container. Reporting
    # every schedule in the fleet as broken because tzdata is missing
    # would be the worst possible failure mode for an advisory panel.
    monkeypatch.setattr(u, "available_timezones", lambda: set())
    notes = u.schedule_notes("rabbit", dict(GOOD, timeZone="Europe/Madird"))
    assert notes == []


def test_the_real_runtime_has_a_tz_database():
    # Guards the test above from becoming vacuous: if the image ever
    # ships without tzdata, the check silently stops working and this is
    # what says so. Verified in the pinned python:3.12-slim image, which
    # carries 486 zones.
    assert u._known_zone("Europe/Madrid") is True


@pytest.mark.parametrize("junk", [
    None, "", "not a cron", "0 21 * *", "0 21 * * * *", 42, [], {},
])
def test_unparseable_input_never_raises(junk):
    # Anything malformed enough to reach here was already refused by the
    # chart. Crashing would take down the whole comment for a change the
    # panel was only annotating.
    assert isinstance(u.schedule_notes("rabbit", {"stop": junk}), list)


def test_a_non_mapping_schedule_is_ignored():
    assert u.schedule_notes("rabbit", "0 21 * * *") == []
    assert u.schedule_notes("rabbit", None) == []


# --------------------------------------------------------------------
# Scoping: only roles this PR actually edits.
# --------------------------------------------------------------------

PREFIX = "appspace.infra.deployLinuxServicesK8s."


def test_only_the_edited_role_is_read():
    flat = {
        PREFIX + "rabbit.uptimeSchedule.stop": "0 21 * * *",
        PREFIX + "rabbit.uptimeSchedule.start": "0 21 * * *",   # identical
        PREFIX + "rabbit.uptimeSchedule.timeZone": "Europe/Madrid",
        PREFIX + "mongo.uptimeSchedule.stop": "0 21 * * *",
        PREFIX + "mongo.uptimeSchedule.start": "0 21 * * *",    # also bad
        PREFIX + "mongo.uptimeSchedule.timeZone": "Europe/Madrid",
    }
    # Only rabbit's schedule is touched by this pull request.
    notes = u.notes_for_changed_roles(
        flat, [PREFIX + "rabbit.uptimeSchedule.start"], PREFIX)
    assert len(notes) == 1
    assert "`rabbit`" in notes[0]
    assert "mongo" not in "".join(notes)


def test_a_pr_touching_no_schedule_produces_nothing():
    flat = {PREFIX + "rabbit.uptimeSchedule.stop": "0 21 21 * 5",
            PREFIX + "rabbit.uptimeSchedule.timeZone": "Europe/Madrid"}
    notes = u.notes_for_changed_roles(
        flat, [PREFIX + "rabbit.machineType"], PREFIX)
    assert notes == []


def test_a_removed_schedule_reports_nothing():
    # The keys changed, but nothing is left to comment on. Removal is a
    # legitimate edit and the panel's own bullets already state it.
    notes = u.notes_for_changed_roles(
        {}, [PREFIX + "rabbit.uptimeSchedule.stop"], PREFIX)
    assert notes == []


# --------------------------------------------------------------------
# The call site. The notes must reach the panel's ROUTINE list, because
# routine lines comment and dangerous lines block.
# --------------------------------------------------------------------

import diff_preview as m

_YAML_BAD = """
appspace:
  infra:
    deployLinuxServicesK8s:
      rabbit:
        enabled: true
        uptimeSchedule:
          stop: "0 21 * * *"
          start: "0 21 * * *"
          timeZone: "Europe/Madrid"
"""
_YAML_BASE = """
appspace:
  infra:
    deployLinuxServicesK8s:
      rabbit:
        enabled: true
"""


def _panel(monkeypatch, new_yaml, old_yaml=_YAML_BASE):
    def fake_fetch(path, sha, repo=None):
        return (new_yaml if sha == "newsha" else old_yaml), m.BB_OK
    monkeypatch.setattr(m, "_bb_fetch_cached", fake_fetch)
    return m._summarize_vm_changes(
        ["gcp/prod/private-cloud/na1/pv-x-a/customer.yaml"],
        "newsha", "basesha",
        {"gcp/prod/private-cloud/na1/pv-x-a/customer.yaml": True},
        {})


def test_the_note_reaches_the_panel(monkeypatch):
    panel = "\n".join(_panel(monkeypatch, _YAML_BAD))
    assert "prioritises the stop" in panel


def test_notes_never_reach_the_dangerous_list(monkeypatch):
    # The whole safety property: a schedule note must never turn the
    # panel into its blocking form. _vm_panel_lines emits the danger
    # header only when the dangerous list is non-empty, so the panel
    # opening with the routine header proves the note stayed routine.
    #
    # Asserted on the FIRST LINE rather than with `not in`: the danger
    # header ("## ...CHANGES") is a substring of the routine one
    # ("### ...CHANGES (routine)"), so a containment check reports a
    # correctly-routine panel as dangerous. That false failure is how
    # this comment came to be written.
    panel_lines = _panel(monkeypatch, _YAML_BAD)
    assert panel_lines[0] == m._VM_PANEL_ROUTINE_HDR
    panel = "\n".join(panel_lines)
    assert "blocked from merging" not in panel.lower()
    # The danger banner names VMs explicitly; routine panels never carry it.
    assert "verify every line below before merging" not in panel.lower()


def test_a_clean_schedule_adds_no_note(monkeypatch):
    good = _YAML_BAD.replace('start: "0 21 * * *"', 'start: "0 7 * * *"')
    panel = "\n".join(_panel(monkeypatch, good))
    assert "uptime schedule:" not in panel
