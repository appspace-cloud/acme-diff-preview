"""Advisory reading of VM uptime schedules (COPS-2714).

A `uptimeSchedule` under a linux-services role parks a VM on a daily
window straight from customer.yaml:

    deployLinuxServicesK8s:
      rabbit:
        uptimeSchedule:
          stop:  "0 21 * * *"
          start: "0 7 * * *"
          timeZone: "Europe/Madrid"

The chart already refuses the shapes it can be certain about — clock
times, wrong field counts, minute 60, hour 25, a missing or non-IANA
time zone, a schedule coexisting with desiredStatus. Those fail at
render and the merge is blocked by the diff status, so nothing here
repeats them.

What this module reads is the layer underneath: expressions the chart
renders happily, GCP accepts without complaint, and that still do not
mean what the author meant. They were catalogued against the GCP
instance-schedule documentation and a 25-case render matrix; each note
below exists because GCP's own behaviour makes the mistake silent.

EVERY note here is advisory and none of them blocks. That is the whole
design, not a limitation: for almost every suspicious shape a
legitimate twin exists — a Friday stop with a Monday start is a weekend
park, a five-minute gap is a nightly bounce, an inverted day/night
window is a batch runner. A checker that blocked on those would be
wrong often enough that people would learn to route around it, and the
one thing worse than no guardrail is a guardrail nobody trusts. So the
notes state what GCP will do and let the author decide.

Two of them (an unknown time zone, an out-of-range calendar field) are
certain rather than suspicious. They stay advisory anyway: they fail at
apply time in GCP, where the environment goes Degraded in Argo, and a
comment that says so before the merge is worth more than a block that
would also catch the rare legitimate case this module cannot foresee.
"""

import re

try:                                   # stdlib since 3.9
    from zoneinfo import ZoneInfo, available_timezones
except ImportError:                    # pragma: no cover - 3.8 and older
    ZoneInfo = None

    def available_timezones():
        return set()

from datetime import datetime

# GCP applies a scheduled operation up to 15 minutes late, and documents
# that a start and a stop closer together than that may execute out of
# order — "the stop operation might occur before the start operation,
# preventing the start operation from happening".
_JITTER_MIN = 15

# The three fields the chart deliberately leaves loose. Minute and hour
# are range-checked at render because they are the two a person types;
# these are not, so a 32nd day of the month reaches GCP and is refused
# there, after the merge.
_FIELD_RANGES = {2: ("day-of-month", 1, 31),
                 3: ("month", 1, 12),
                 4: ("day-of-week", 0, 7)}

# Days in each month, taking February at its longest: a schedule is a
# recurring rule, so 29 February is legal (it fires in leap years) while
# 30 February can never fire at all.
_MONTH_LEN = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
              7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

_PLAIN_INT = re.compile(r"^\d+$")


def _fields(expr):
    """Split a cron expression, or None when it is not five fields."""
    if not isinstance(expr, str):
        return None
    parts = expr.split()
    return parts if len(parts) == 5 else None


def _single_int(field):
    """The int a field pins to, or None when it is *, a range or a step."""
    if field is None or not _PLAIN_INT.match(field):
        return None
    return int(field)


def _restricted(field):
    """True when the field narrows anything at all (is not bare `*`)."""
    return isinstance(field, str) and field.strip() != "*"


def _observes_dst(tz_name):
    """True when the zone shifts its offset between January and July."""
    if ZoneInfo is None:
        return False
    try:
        tz = ZoneInfo(tz_name)
        jan = datetime(2026, 1, 15, 12, tzinfo=tz).utcoffset()
        jul = datetime(2026, 7, 15, 12, tzinfo=tz).utcoffset()
    except Exception:
        return False
    return jan is not None and jul is not None and jan != jul


def _known_zone(tz_name):
    """Whether the runtime's tz database contains this zone.

    Returns None — not False — when the database is unavailable, so a
    container shipped without tzdata stays silent instead of reporting
    every schedule in the fleet as broken. Absence of the database is a
    fact about the container, never about the config.
    """
    try:
        zones = available_timezones()
    except Exception:
        return None
    if not zones:
        return None
    return tz_name in zones


def _minutes(field_min, field_hour):
    """Minute-of-day for a schedule that pins both, else None."""
    m, h = _single_int(field_min), _single_int(field_hour)
    if m is None or h is None:
        return None
    return h * 60 + m


def schedule_notes(role, sched):
    """Advisory lines for one role's resulting uptimeSchedule.

    `sched` is the merged mapping as it will exist after the pull request
    ({"stop": ..., "start": ..., "timeZone": ...}); keys may be missing.
    Returns a list of markdown fragments, empty when nothing is worth
    saying. Never raises: a schedule this cannot parse is one the chart
    has already refused, and a crash here would take down the whole
    comment for a change the panel was only annotating.
    """
    if not isinstance(sched, dict):
        return []
    stop = sched.get("stop")
    start = sched.get("start")
    tz = sched.get("timeZone")
    notes = []

    def say(text):
        notes.append(f"- ℹ️ `{role}` · uptime schedule: {text}")

    # --- the two certain ones ------------------------------------------
    if isinstance(tz, str) and tz.strip():
        known = _known_zone(tz.strip())
        if known is False:
            say(f"`{tz}` is not a zone in the IANA database. The chart only "
                f"checks the *shape* of a time zone, so a plausible "
                f"misspelling renders fine and GCP refuses it at apply "
                f"time, leaving the environment Degraded in Argo.")

    for label, expr in (("stop", stop), ("start", start)):
        f = _fields(expr)
        if not f:
            continue
        for idx, (name, lo, hi) in _FIELD_RANGES.items():
            v = _single_int(f[idx])
            if v is not None and not (lo <= v <= hi):
                say(f"`{label}` has `{f[idx]}` as its {name}, outside "
                    f"{lo}–{hi}. The chart range-checks only minute and "
                    f"hour, so this reaches GCP and is refused there.")
        dom, month = _single_int(f[2]), _single_int(f[3])
        if (dom is not None and month is not None
                and month in _MONTH_LEN and dom > _MONTH_LEN[month]):
            say(f"`{label}` is set for day {dom} of month {month}, a date "
                f"that does not exist. This schedule can never fire.")

    # --- the suspicious ones, each with a legitimate twin ---------------
    if isinstance(stop, str) and isinstance(start, str):
        if stop.split() == start.split() and _fields(stop):
            say(f"`stop` and `start` are the same expression "
                f"(`{stop}`). GCP prioritises the stop and ignores the "
                f"start, so the VM stays parked — with no error "
                f"anywhere.")
        else:
            fs, fa = _fields(stop), _fields(start)
            if fs and fa and fs[2:] == fa[2:]:
                # Same calendar, so the two times are comparable; a gap
                # across different day sets is not.
                a, b = _minutes(fs[0], fs[1]), _minutes(fa[0], fa[1])
                if a is not None and b is not None:
                    gap = min((a - b) % 1440, (b - a) % 1440)
                    if 0 < gap < _JITTER_MIN:
                        say(f"`stop` and `start` are {gap} minutes apart. "
                            f"GCP applies a scheduled operation up to "
                            f"{_JITTER_MIN} minutes late and documents that "
                            f"a pair this close can run out of order, "
                            f"skipping the start. A deliberate nightly "
                            f"bounce is fine — this is only worth a "
                            f"second look.")

    for label, expr in (("stop", stop), ("start", start)):
        f = _fields(expr)
        if not f:
            continue
        if _restricted(f[2]) and _restricted(f[4]):
            say(f"`{label}` restricts both day-of-month (`{f[2]}`) and "
                f"day-of-week (`{f[4]}`). Cron treats that as a UNION, not "
                f"an intersection — it fires on both, not on days that "
                f"satisfy both.")
        hour = _single_int(f[1])
        if (hour == 2 and isinstance(tz, str) and _observes_dst(tz.strip())):
            say(f"`{label}` fires in the 02:00 hour, which `{tz}` skips on "
                f"the spring DST night and repeats on the autumn one. GCP "
                f"runs the operation offset or, when a start and a stop "
                f"collide, drops the start.")

    if start and not stop:
        say("this is a start-only schedule. Nothing then enforces the "
            "power state — the VM is started daily but never parked, "
            "and `desiredStatus` is omitted while a schedule exists. A "
            "daily 'make sure it is up' guard is a real pattern; a "
            "half-written window is the likelier reading.")

    return notes


def notes_for_changed_roles(new_flat, changed_keys, prefix):
    """Advisory lines for every role whose schedule this PR touches.

    Scoped to roles the pull request actually edits: a panel that
    re-litigated every existing schedule on every unrelated PR would be
    noise, and noise is what teaches people to skim past the section.
    """
    marker = ".uptimeSchedule."
    roles = []
    for k in changed_keys:
        if not k.startswith(prefix) or marker not in k:
            continue
        role = k[len(prefix):].split(".", 1)[0]
        if role not in roles:
            roles.append(role)
    out = []
    for role in roles:
        base = f"{prefix}{role}{marker}"
        sched = {leaf: new_flat[key]
                 for key, leaf in ((base + s, s)
                                   for s in ("stop", "start", "timeZone"))
                 if key in new_flat and new_flat[key] is not None}
        out.extend(schedule_notes(role, sched))
    return out
