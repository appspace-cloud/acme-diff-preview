"""COPS-2671, slice B: five dark corners of the decommission and render-cache paths.

Every line covered here was dark for the same underlying reason: it only
executes when something *unusual* has already happened, and the suite's
existing fixtures short-circuit before reaching it.

  1. `_render_main_side_resources` tail (parse / cache-put / return).
     The decommission inventory reuses `_main_render_cache`, and the suite's
     render-cache directory is a real, process-global path under
     `/tmp/acme-helm-cache` that survives between runs. Once ANY run has
     stored an entry, the existing test warms straight into the disk tier and
     the cold-path tail never runs again. Pinned here against a throwaway
     cache dir so the cold path is genuinely cold every time.

  2. `_cascade_finalizer_live([])` -> None.
     "Could not tell", deliberately not False. False drives a block, and a
     block on an unanswerable question is how an ArgoCD outage would stop
     every decommission review. No candidate ever carries an empty app list,
     so nothing in the suite asked the function a question it cannot answer.

  3. The VM-arming `except` in `_evaluate_env_decommissions` (COPS-2650).
     Reached only by an identity file that is fetchable but not parseable —
     a truncated or half-merged customer.yaml. Both flags must stay False so
     Phase 1 renders as pending/not-applicable rather than claiming "done",
     and the reason has to be logged or the phase table just looks wrong for
     no visible cause.

  4. The `+N more kind(s)` tail on the resource breakdown.
     Only fires past eight distinct kinds. The fixtures in the suite render
     two or three, so the truncation that exists because PR #3894 wrapped ~30
     kinds over eight lines was never exercised.

  5. The whole COPS-2631 shadow-audit block in `_run_one_diff`.
     Gated behind `MAIN_RENDER_CACHE_SHADOW_RATE` (1% in production) AND a
     cache hit, so a unit run practically never samples it. It is also the
     block with the most consequence per line: it is what turns a poisoned
     cache entry into a correct diff, and COPS-2645 added the discard that
     stops a bad durable object re-infecting every fresh pod. All three
     tiers are real here (memory, a tmp disk dir, a fake bucket) so the
     discard is checked by the state it leaves behind, and both ways helm
     can fail mid-audit — raising, and returning an error beside partial
     output — are driven, because only one of them is obvious.

Everything below asserts the observable consequence — the diff text, the
rendered panel, the counter, the log — not that a call returned.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as m  # noqa: E402
import diff_ui            # noqa: E402
import logsink            # noqa: E402
import render_cache       # noqa: E402


APP = "pv-cov2671b-a-ms"
REG = "registry.example.com"
CHART = "appspace-ms"
NS = "pv-cov2671b-a"

MAIN_SHA = "cov2671bmain"
PR_SHA = "cov2671bpr01"


def _manifest(replicas):
    return (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        f"  name: web\n"
        f"  namespace: {NS}\n"
        "spec:\n"
        f"  replicas: {replicas}\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: web\n"
        f"  namespace: {NS}\n"
    )


@pytest.fixture()
def cold_render_cache(tmp_path, monkeypatch):
    """A render cache with nothing in any tier, local to this test.

    The module-level default points at /tmp/acme-helm-cache/main-renders,
    which outlives the process; a warm entry there is exactly what hides the
    cold path this file is here to cover.
    """
    monkeypatch.setattr(render_cache, "MAIN_RENDER_CACHE_DIR",
                        str(tmp_path / "renders"))
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET", "")
    with m._main_render_lock:
        m._main_render_cache.clear()
    yield tmp_path
    with m._main_render_lock:
        m._main_render_cache.clear()


@pytest.fixture()
def chart_dir(tmp_path):
    """A real on-disk chart tree: the content key hashes it."""
    d = tmp_path / "chart"
    (d / "templates").mkdir(parents=True)
    (d / "Chart.yaml").write_text("apiVersion: v2\nname: appspace-ms\nversion: 1.0.0\n")
    (d / "templates" / "deploy.yaml").write_text("kind: Deployment\n")
    return d


@pytest.fixture()
def app_metadata(monkeypatch, chart_dir):
    monkeypatch.setitem(m._app_chart_map, APP, CHART)
    monkeypatch.setitem(m._app_chart_revision_map, APP, "2603.0.1")
    monkeypatch.setitem(m._app_chart_registry_map, APP, REG)
    monkeypatch.setitem(m._app_value_files_map, APP,
                        [f"$config/gcp/dev/x/{NS}/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, APP, NS)
    monkeypatch.setattr(m, "_ensure_chart", lambda *a, **k: str(chart_dir))


# ── 1. _render_main_side_resources: the cold-cache tail ──────────────────

def test_render_main_side_resources_parses_and_writes_through_on_a_cold_cache(
        cold_render_cache, app_metadata, monkeypatch):
    """The decommission inventory's own render path, with nothing cached.

    Two facts are asserted: the raw YAML is turned into the keyed resource
    dict the inventory counts (so the caller can size the blast radius), and
    the result is written through so a sibling app of the same environment —
    glb/ms/ss are usually all in the same PR — does not pay for it again.
    """
    calls = []

    def fake_template(chart, release, namespace, vals):
        calls.append((release, namespace))
        return _manifest(2), None

    monkeypatch.setattr(m, "_fetch_value_files",
                        lambda files, sha: {f: "appspace: {}\n" for f in files})
    monkeypatch.setattr(m, "_helm_template", fake_template)

    res = m._render_main_side_resources(APP, MAIN_SHA)

    assert set(res) == {("apps/Deployment", NS, "web"), ("Service", NS, "web")}, \
        "the raw render must come back as the keyed resource dict the inventory counts"
    assert "replicas: 2" in res[("apps/Deployment", NS, "web")]
    assert calls == [(APP.split("/")[-1], NS)], "exactly one render for a cold cache"

    # Write-through, memory tier: the second call must not render again.
    again = m._render_main_side_resources(APP, MAIN_SHA)
    assert again == res
    assert len(calls) == 1, "the parsed result was not put into the cache"

    # Write-through, disk tier: drop memory and it must still be served
    # without a render (this is what survives an app-cache reload mid-run).
    with m._main_render_lock:
        m._main_render_cache.clear()
    from_disk = m._render_main_side_resources(APP, MAIN_SHA)
    assert from_disk == res
    assert len(calls) == 1, "the raw render was not written through to disk"


# ── 2. _cascade_finalizer_live: no apps means "could not tell" ───────────

def test_cascade_finalizer_check_with_no_apps_is_unknown_not_false():
    """None and False are different answers and drive different panels.

    False means "ArgoCD does not have the finalizer" and raises the 🚨 block
    that tells a reviewer the cascade is a lie. With nothing to ask about,
    the honest answer is None — and _cascade_mismatch_note must stay silent.
    """
    assert m._cascade_finalizer_live([]) is None
    assert m._cascade_finalizer_live([]) is not False, \
        "an unanswerable check must never be reported as a missing finalizer"
    assert m._cascade_mismatch_note("pv-cov2671b-a", [], True) == [], \
        "an unknown finalizer state must not raise the mismatch alarm"


# ── 3+4. the folder-removal panel: unreadable VM state, and >8 kinds ─────

IDENT = "gcp/prod/private-cloud/eu1-b/monthly/pv-cov2671b-a/customer.yaml"

VM_ARMED = (
    "appspace:\n"
    "  customerName: cov2671b\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      defaults:\n"
    "        allowDeletion: true\n"
    "      svc:\n"
    "        enabled: true\n"
)
# Same VMs declared, deletion NOT armed: the case Phase 1 exists to report.
# Under the abandon policy the real VM, its data disk and its reserved IP
# survive the cascade, so this row must read pending, never done.
VM_DECLARED_NOT_ARMED = (
    "appspace:\n"
    "  customerName: cov2671b\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      defaults:\n"
    "        allowDeletion: false\n"
    "      svc:\n"
    "        enabled: true\n"
)
# An environment with no linux service VMs at all: delete.md says to skip
# Phase 1, and the row says so instead of inventing a state for it.
VM_NONE_DECLARED = (
    "appspace:\n"
    "  customerName: cov2671b\n"
)
# The armed file truncated mid flow-mapping — a half-written or badly merged
# customer.yaml. Fetchable (BB_OK), and PyYAML raises on it.
VM_ARMED_TRUNCATED = (
    "appspace:\n"
    "  customerName: cov2671b\n"
    "  infra:\n"
    "    deployLinuxServicesK8s:\n"
    "      defaults: {allowDeletion: true\n"
)


def _candidate(apps=("pv-cov2671b-a-ms",)):
    return {"env_name": "pv-cov2671b-a", "identity_file": IDENT,
            "apps": list(apps), "env_dir": os.path.dirname(IDENT)}


def _removal_panel(monkeypatch, base_content, sha_tag, resources=None):
    """Render the Phase-3 (folder removal) panel for one candidate.

    Fresh shas per call: the fetch layer memoises on (path, sha), so reusing
    them would serve a previous fixture's content back.
    """
    base, head = "base" + sha_tag, "head" + sha_tag

    def fake_fetch(path, sha, repo=None):
        if sha == head:
            return (None, m.BB_NOT_FOUND)
        return (base_content, m.BB_OK)

    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(
        m, "_render_main_side_resources",
        lambda app, sha: (resources if resources is not None
                          else {("apps/Deployment", NS, "web"): _manifest(1)}))
    lines, envs = m._evaluate_env_decommissions([_candidate()], head, base)
    assert envs == ["pv-cov2671b-a"], "the confirmed deletion must be reported"
    return "\n".join(lines)


def _phase1_row(panel):
    rows = [l for l in panel.splitlines() if l.startswith("| **Phase 1")]
    assert rows, "the panel must carry a Phase 1 row:\n" + panel
    return rows[0]


def _phase1_state(panel):
    """The State cell of the Phase 1 row — the arming decision itself.

    Asserting on the cell rather than on "'done' is somewhere in the row"
    is what makes the three states distinguishable: pending, not applicable
    and done are three different answers, and a test that only rejects the
    word "done" cannot tell the first two apart (nor notice a row that
    silently stopped rendering the state at all).
    """
    row = _phase1_row(panel)
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert len(cells) == 3, "unexpected phase-table row shape: " + row
    return cells[1]


def test_phase1_state_follows_the_arming_flag_not_merely_the_vm_section(monkeypatch):
    """Phase 1 has to report the `allowDeletion` decision, not its presence.

    Both files below declare exactly the same VMs; they differ only in
    whether deletion is armed. A panel that renders "done" for both — or
    that renders the state from a constant — tells a reviewer the real VM,
    its data disk and its reserved IP go with the cascade when in fact they
    are abandoned. That is the whole content of the row, so both directions
    are pinned here, plus the "no VMs declared" case that must not borrow
    either state.
    """
    armed = _removal_panel(monkeypatch, VM_ARMED, "vmok")
    unarmed = _removal_panel(monkeypatch, VM_DECLARED_NOT_ARMED, "vmoff")
    novms = _removal_panel(monkeypatch, VM_NONE_DECLARED, "vmnone")

    assert _phase1_state(armed) == m._PH_DONE, \
        "an armed allowDeletion is Phase 1 done"
    assert _phase1_state(unarmed) == m._PH_PENDING, \
        "VMs declared with allowDeletion false is Phase 1 PENDING, not done"
    assert "done" not in _phase1_row(unarmed), \
        "an unarmed environment must not read as done anywhere in the row"
    assert _phase1_state(novms) == m._PH_NA, \
        "an environment declaring no deployLinuxServicesK8s VMs has nothing to arm"
    assert "declares no" in _phase1_row(novms), \
        "the not-applicable row must say why the phase is skipped"


def test_unparseable_identity_file_fails_phase1_closed_and_says_why(monkeypatch):
    """COPS-2650: an unreadable VM section must never render as Phase 1 done.

    The environment really does declare VMs and really has armed deletion —
    we simply cannot see it. The proof that the parse failure, and not the
    file's content, drives the row is the differential: byte for byte the
    same configuration, once whole and once truncated, must produce two
    different rows. Whole -> done. Truncated -> both flags are thrown away,
    so the panel falls back to the state it can defend, and the reason is
    logged so the phase table is explicable instead of merely wrong-looking.
    """
    debug_lines = []
    monkeypatch.setattr(logsink, "debug",
                        lambda msg, **k: debug_lines.append(msg))

    whole = _removal_panel(monkeypatch, VM_ARMED, "vmwhole")
    panel = _removal_panel(monkeypatch, VM_ARMED_TRUNCATED, "vmbad")

    assert _phase1_state(whole) == m._PH_DONE, \
        "control: the same configuration, parseable, does report done"
    assert _phase1_state(panel) == m._PH_NA, (
        "a bad parse must discard BOTH flags: with declares_vms unknown the "
        "row is the not-applicable row, never a state read off the file")
    assert "done" not in _phase1_row(panel), \
        "an unparseable identity file must not claim the VM deletion is armed"
    assert "ENVIRONMENT DECOMMISSION" in panel, \
        "the deletion is confirmed fact; a bad parse must not suppress the panel"
    reasons = [d for d in debug_lines if "VM arming state unreadable" in d]
    assert reasons, ("the fail-closed decision must be logged, not silent:\n"
                     + "\n".join(debug_lines))
    assert IDENT in reasons[0], "the log must name the file that could not be read"
    assert "failing closed" in reasons[0]


def test_kind_breakdown_past_eight_kinds_is_truncated_with_a_count(monkeypatch):
    """PR #3894: ~30 comma-separated kinds wrapped over eight lines in
    Bitbucket and the tail never drove a decision. Top eight, then a count."""
    # Distinct counts make the ranking unambiguous: 10 kinds, descending.
    resources = {}
    kinds = [f"grp{i}/Kind{i}" for i in range(10)]
    for rank, kind in enumerate(kinds):
        for n in range(10 - rank):        # 10, 9, 8, ... 1
            resources[(kind, NS, f"r{rank}-{n}")] = "kind: x\n"
    total = sum(range(1, 11))

    panel = _removal_panel(monkeypatch, "appspace:\n  customerName: c\n",
                           "kinds", resources=resources)
    inventory = [l for l in panel.splitlines() if "LEFT RUNNING" in l]
    assert inventory, "the orphan inventory line must be present:\n" + panel
    line = inventory[0]

    assert f"{total} total" in line
    assert "10 grp0/Kind0" in line, "the most numerous kind must be shown"
    assert "3 grp7/Kind7" in line, "the eighth-ranked kind must be shown"
    assert "+2 more kind(s)" in line, "the tail past eight must be counted"
    assert "grp8/Kind8" not in line and "grp9/Kind9" not in line, \
        "the truncated tail must not also be printed"


# ── 5. the COPS-2631 shadow audit inside _run_one_diff ───────────────────

MAIN_MARK = "side: main\n"
PR_MARK = "side: pr\n"


class _FakeHelm:
    """A helm whose main-side output can be made to DRIFT between calls.

    That drift is the whole scenario: a cache entry that no longer matches
    what helm produces today. Whether the cause is a non-hermetic chart, a
    salt that should have been bumped or a corrupted object, the audit's job
    is the same — notice, and serve the truth.

    The main side can also be made to fail from the second call on, in
    either of the two ways helm actually fails: by raising, and by coming
    back with an error string beside a partial/garbage document. The second
    is the dangerous one — there IS output, it just is not the truth.
    """

    def __init__(self, main_yaml, pr_yaml):
        self.main_yaml = main_yaml
        self.pr_yaml = pr_yaml
        self.main_calls = 0
        self.pr_calls = 0
        self.main_raises = None
        self.main_err = None          # (yaml, err) returned instead
        self.main_err_yaml = ""

    def __call__(self, chart, release, namespace, vals):
        is_pr = any(PR_MARK.strip() in v for v in vals.values())
        if is_pr:
            self.pr_calls += 1
            return self.pr_yaml, None
        self.main_calls += 1
        if self.main_calls > 1:
            if self.main_raises is not None:
                raise self.main_raises
            if self.main_err is not None:
                return self.main_err_yaml, self.main_err
        return self.main_yaml, None


class _FakeBucket:
    """The durable tier, in memory, installed at the diff_ui._gcs_* seam.

    Without it MAIN_RENDER_GCS_BUCKET is empty, every bucket call returns
    early, and "discarded from every tier" is an untested claim — which is
    exactly the COPS-2645 regression: a poisoned object left in the bucket
    re-infects every fresh pod that warms from it.
    """

    def __init__(self):
        self.objects = {}
        self.deletes = []

    def install(self, monkeypatch):
        monkeypatch.setattr(diff_ui, "_gcs_upload", self._up)
        monkeypatch.setattr(diff_ui, "_gcs_download", self._down)
        monkeypatch.setattr(diff_ui, "_gcs_delete", self._del)
        return self

    def _up(self, bucket, name, data):
        self.objects[name] = data
        return True

    def _down(self, bucket, name):
        return self.objects.get(name)

    def _del(self, bucket, name):
        self.deletes.append(name)
        self.objects.pop(name, None)
        return True

    def raw(self, key):
        """The render text the bucket holds for one cache key, or None."""
        data = self.objects.get(m._main_render_gcs_name(key))
        return None if data is None else m._main_render_gcs_decode(data)


@pytest.fixture()
def shadow_world(cold_render_cache, app_metadata, monkeypatch):
    """_run_one_diff wired to fakes, with the shadow audit always sampling.

    Three tiers are real (memory, a tmp disk dir, a fake bucket) so that
    what the audit does to the cache is observable state, not a call count.
    Each `_main_render_cache_put` snapshots all three FIRST: the put is the
    instant right after a discard, and the only moment at which the
    discard's effect can be seen before the repair refills everything.
    """
    monkeypatch.setattr(m, "MAIN_RENDER_CACHE_SHADOW_RATE", 1.0)
    monkeypatch.setattr(render_cache, "MAIN_RENDER_GCS_BUCKET",
                        "cov2671b-durable")
    bucket = _FakeBucket().install(monkeypatch)

    def fake_fetch(files, sha):
        mark = PR_MARK if sha == PR_SHA else MAIN_MARK
        return {f: "appspace: {}\n" + mark for f in files}

    monkeypatch.setattr(m, "_fetch_value_files", fake_fetch)
    helm = _FakeHelm(_manifest(2), _manifest(3))
    monkeypatch.setattr(m, "_helm_template", helm)

    discarded = []
    real_discard = m._main_render_cache_discard

    def spy_discard(key):
        discarded.append(key)
        real_discard(key)

    monkeypatch.setattr(m, "_main_render_cache_discard", spy_discard)

    puts = []
    real_put = m._main_render_cache_put

    def spy_put(key, raw, resources):
        with m._main_render_lock:
            in_memory = key in m._main_render_cache
        puts.append({"key": key, "raw": raw,
                     "disk": m._main_render_disk_load(key),
                     "memory": in_memory,
                     "bucket": bucket.raw(key)})
        real_put(key, raw, resources)

    monkeypatch.setattr(m, "_main_render_cache_put", spy_put)

    helm.discarded = discarded
    helm.puts = puts
    helm.bucket = bucket
    yield helm
    m._main_render_gcs_flush()


def _mismatches():
    with m._diff_stats_lock:
        return m._diff_stats.get("main_render_cache_shadow_mismatches", 0)


def test_shadow_audit_heals_a_drifted_cache_entry_within_the_same_diff(
        shadow_world, monkeypatch):
    """The point of the audit: a stale entry must not produce a stale diff.

    Run 1 renders main as `replicas: 2` and caches it. Between the runs the
    truth moves to `replicas: 9`. Run 2 hits the cache, so without the audit
    it would confidently report a 2 -> 3 change that never existed. The audit
    re-renders, sees the bytes differ, throws the entry out of every tier and
    diffs against the fresh render instead.
    """
    helm = shadow_world
    logged = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, sev="INFO", **k: logged.append((sev, msg)))

    diff_text, reason, detail, _vc = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert reason is None and "replicas: 2" in diff_text
    assert helm.main_calls == 1, "run 1 is a cache miss and must render main once"

    # Run 1 populated all three tiers with what is about to become stale.
    key = helm.puts[0]["key"]
    m._main_render_gcs_flush()             # the mirror upload is off the diff path
    assert m._main_render_disk_load(key) == _manifest(2)
    assert helm.bucket.raw(key) == _manifest(2), \
        "the durable tier must hold the stale bytes, or the discard below proves nothing"

    before = _mismatches()
    helm.main_yaml = _manifest(9)          # the cached bytes are now wrong

    diff_text, reason, detail, _vc = m._run_one_diff(APP, PR_SHA, MAIN_SHA)

    assert reason is None, detail
    assert helm.main_calls == 2, "the sampled hit must be re-rendered for audit"
    assert "replicas: 9" in diff_text, \
        "the diff must be computed against the fresh render, not the stale entry"
    assert "replicas: 2" not in diff_text, \
        "the discarded, stale main side leaked into the reported diff"
    assert _mismatches() == before + 1, "the mismatch must be counted"
    errors = [msg for sev, msg in logged if sev == "ERROR" and "SHADOW MISMATCH" in msg]
    assert errors, f"a mismatch must be logged at ERROR: {logged}"
    assert APP in errors[0]

    # COPS-2645: the entry must be GONE from every tier, not merely
    # overwritten. The repair write is the instant to look: anything the
    # discard failed to remove is still sitting there, holding replicas: 2.
    assert len(helm.puts) == 2, f"the corrected render must be written back: {helm.puts}"
    repair = helm.puts[1]
    assert repair["key"] == key and repair["raw"] == _manifest(9)
    assert repair["memory"] is False, "the poisoned entry survived in memory"
    assert repair["disk"] is None, \
        "the poisoned entry survived on disk; a pod-local read would re-serve it"
    assert repair["bucket"] is None, \
        ("COPS-2645: the poisoned object survived in the bucket and would "
         "re-infect every fresh pod that warms from it")

    # ...and the corrected render replaces it in the durable tiers too.
    m._main_render_gcs_flush()
    assert m._main_render_disk_load(key) == _manifest(9), \
        "the cache must be repaired with the fresh render, not merely emptied"
    assert helm.bucket.raw(key) == _manifest(9), \
        "the bucket must be repaired as well, or the next pod warms into the past"


def test_shadow_audit_leaves_a_matching_entry_alone(shadow_world):
    """The negative control. An audit that fired on every hit would discard
    the cache it exists to protect, and the counter would be meaningless."""
    helm = shadow_world
    m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    before = _mismatches()

    diff_text, reason, _d, _vc = m._run_one_diff(APP, PR_SHA, MAIN_SHA)

    assert reason is None
    assert helm.main_calls == 2, "the hit is still audited"
    assert _mismatches() == before, "identical bytes are not a mismatch"
    assert helm.discarded == [], "a healthy entry must survive the audit"
    assert helm.bucket.deletes == [], "no bucket object may be deleted on a match"
    key = helm.puts[0]["key"]
    assert len(helm.puts) == 1, "a matching entry needs no rewrite"
    assert m._main_render_disk_load(key) == _manifest(2), \
        "the healthy entry must still be on disk after the audit"
    assert "replicas: 2" in diff_text and "replicas: 3" in diff_text


@pytest.mark.parametrize("mode", ["raises", "returns_error"])
def test_shadow_audit_failure_never_fails_the_diff(shadow_world, monkeypatch, mode):
    """The audit is an optional 1% sample, and helm fails in two ways.

    `raises` is the obvious one — the whole block is wrapped, so the cost is
    a log line, not the reviewer's comment.

    `returns_error` is the one that can do damage: helm exits non-zero and
    still prints something. That output is a partial or empty render, NOT
    ground truth, and it will not match the cache. If the audit compares it
    anyway it "finds" a mismatch on a perfectly healthy entry: it throws the
    entry out of every tier, counts a mismatch, and then diffs the reviewer's
    PR against the garbage. So both channels must end the same way — the
    served entry survives untouched and the diff is the cached one.
    """
    helm = shadow_world
    m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    key = helm.puts[0]["key"]
    m._main_render_gcs_flush()

    logged = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, sev="INFO", **k: logged.append((sev, msg)))
    before = _mismatches()
    if mode == "raises":
        helm.main_raises = RuntimeError("helm vanished mid-audit")
    else:
        # Different bytes from the cached render, so an audit that trusted
        # this would be certain it had caught drift.
        helm.main_err_yaml = _manifest(9)
        helm.main_err = "Error: template: appspace-ms/deploy.yaml:7: nil pointer"

    diff_text, reason, detail, _vc = m._run_one_diff(APP, PR_SHA, MAIN_SHA)

    assert reason is None, f"the audit must not fail the diff: {detail}"
    assert helm.main_calls == 2, "the audit really did re-render (and fail)"
    assert "replicas: 2" in diff_text and "replicas: 3" in diff_text, \
        "the served cache entry must still produce its diff"
    assert "replicas: 9" not in diff_text, \
        "a FAILED render was treated as the truth and diffed against"
    assert _mismatches() == before, "a failed audit proves nothing either way"
    assert helm.discarded == [], "an unproven entry must not be thrown away"

    # Nothing may have moved in any tier: no delete, no rewrite.
    assert helm.bucket.deletes == [], \
        "a failed audit must not delete the durable object"
    assert len(helm.puts) == 1, "a failed audit must not rewrite the entry"
    assert m._main_render_disk_load(key) == _manifest(2), \
        "the healthy entry must survive a failed audit intact"
    assert helm.bucket.raw(key) == _manifest(2)
    assert not [msg for sev, msg in logged
                if sev == "ERROR" and "SHADOW MISMATCH" in msg], \
        f"a failed render is not a mismatch and must not be reported as one: {logged}"

    if mode == "raises":
        warns = [msg for sev, msg in logged
                 if sev == "WARNING" and "shadow audit failed" in msg]
        assert warns, f"the failure must be reported as non-fatal: {logged}"
        assert "helm vanished mid-audit" in warns[0]
