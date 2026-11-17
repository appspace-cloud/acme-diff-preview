"""COPS-3099: a private cloud retired for good revokes its Constellation licence.

With `decommission` and `decommissionPurgeData` armed, the appspace-client-admins
CronJob of the supporting-services app revokes the licence through
license-service, and its pass fails until that is done. Argo CD (3.5.2) shows
the CronJob Healthy with "CronJob has completed its last execution
successfully" once a pass succeeds after a failed one. It also shows Healthy,
with other messages, while a pass runs, before any pass succeeded and while the
CronJob is suspended, and right after the arming sync it can still show an old
invite pass. So the folder-removal PR waits for that exact message, at least 3
minutes after the last sync, on a cloud whose chart renders LICENCE_RETIRE.

A chart line without the revoke renders no LICENCE_RETIRE. Its folder removal
gets a note and no gate, so retirements on other release lines keep working.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
DONE = "CronJob has completed its last execution successfully"
ENV = "pv-foo-c"
IDENT = "gcp/prod/private-cloud/na2-a/monthly/pv-foo-c/customer.yaml"
LIVE = "appspace:\n  customerName: foo\n"
ARMED = LIVE + "  decommission: true\n"
PURGE = ARMED + "  decommissionPurgeData: true\n"


@pytest.fixture(autouse=True)
def no_vertex(monkeypatch):
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)


def cronjob_doc(retire=True, name="appspace-client-admins"):
    env = '            - name: REGISTRATION_TASK\n              value: "client-admins"\n'
    if retire:
        env += '            - name: LICENCE_RETIRE\n              value: "true"\n'
    return ("apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: " + name + "\n"
            "spec:\n  schedule: \"* * * * *\"\n  jobTemplate:\n    spec:\n      template:\n"
            "        spec:\n          containers:\n          - name: appspace-client-admins-job\n"
            "            env:\n" + env)


def ss_resources(retire=True, name="appspace-client-admins"):
    return {("batch/CronJob", ENV, name): cronjob_doc(retire, name)}


def app(health="Healthy", message=DONE, minutes_ago=30, sync="Synced", cron=True):
    # The ServiceAccount, Role and RoleBinding share the CronJob's name and have no health.
    resources = [{"kind": "Deployment", "name": "acme-ping-scaler", "health": {"status": "Healthy"}},
                 {"kind": "ServiceAccount", "name": "appspace-client-admins"},
                 {"group": "rbac.authorization.k8s.io", "kind": "Role", "name": "appspace-client-admins"}]
    if cron:
        resources.append({"group": "batch", "kind": "CronJob", "name": "appspace-client-admins",
                          "health": {"status": health, "message": message}})
    finished = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"metadata": {"name": f"{ENV}-ss"},
            "status": {"sync": {"status": sync}, "resources": resources,
                       "operationState": {"phase": "Succeeded", "finishedAt": finished}}}


# ── the live state ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,state,revoked", [
    ("revoked, last pass green", app(), True),
    ("last pass failed", app("Degraded", "CronJob has not completed its last execution successfully"), False),
    ("pass running", app(message="The job is running. Its last execution may not have been successful"), False),
    ("never succeeded", app(message="The CronJob never completed successfully. It may not be healthy"), False),
    ("suspended", app(message="CronJob is Suspended"), False),
    ("arming synced 1 minute ago, old green pass", app(minutes_ago=1), False),
    ("arming not synced", app(sync="OutOfSync"), False),
    ("no CronJob in the Application", app(cron=False), False),
    ("lookup failed", None, False),
    ("no sync finished yet", {"status": {"sync": {"status": "Synced"}, "resources": app()["status"]["resources"]}},
     False),
])
def test_the_licence_counts_as_revoked_only_on_the_exact_success_message(name, state, revoked):
    got, why = m._licence_revoke_state(state, NOW.timestamp())
    assert got is revoked, name
    assert why


def test_the_settle_time_is_three_minutes():
    assert m._licence_revoke_state(app(minutes_ago=3), NOW.timestamp())[0] is True
    assert m._licence_revoke_state(app(minutes_ago=2), NOW.timestamp())[0] is False


# ── does this chart revoke the licence? ──────────────────────────────────

def test_the_render_tells_whether_the_chart_revokes():
    assert m._renders_licence_revoke(ss_resources()) is True
    assert m._renders_licence_revoke(ss_resources(retire=False)) is False
    assert m._renders_licence_revoke(ss_resources(name="appspace-registration")) is False
    assert m._renders_licence_revoke({}) is False
    assert m._renders_licence_revoke({("batch/CronJob", ENV, "appspace-client-admins"): "kind: [broken"}) is False


# ── the folder-removal PR ────────────────────────────────────────────────

def _evaluate(monkeypatch, base_yaml, ss=None, live=None, ss_fails=False, lookups=None, current=True,
              apps=None):
    with m._vf_cache_lock:
        m._vf_cache.clear()
    m._yaml_cache.clear()
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda p, s, **kw: (None, m.BB_NOT_FOUND) if s == "prsha" else (base_yaml, m.BB_OK))

    def render(a, sha):
        if a.endswith("-ss"):
            if ss_fails:
                raise RuntimeError("chart pull failed")
            return ss if ss is not None else {}
        return {}
    monkeypatch.setattr(m, "_render_main_side_resources", render)
    monkeypatch.setattr(m, "_cascade_finalizer_live", lambda apps: True)
    monkeypatch.setattr(m, "_teardown_hold_met", lambda *a, **k: True)
    monkeypatch.setattr(m, "_licence_clock", lambda: NOW.timestamp())
    calls = lookups if lookups is not None else []
    monkeypatch.setattr(m, "_licence_app_json", lambda a: calls.append(a) or live)
    monkeypatch.setattr(m, "_licence_live_is_current", lambda app_json, ident, sha: current)
    cand = {"env_name": ENV, "identity_file": IDENT,
            "apps": apps if apps is not None else [f"{ENV}-ms", f"{ENV}-ss", f"{ENV}-glb"]}
    lines, envs = m._evaluate_env_decommissions([cand], "prsha", "mainsha")
    assert envs == [ENV]
    return cand, "\n".join(lines)


def kinds(cand):
    return [g["kind"] for g in cand["gates"]]


def test_purge_with_the_revoke_not_finished_is_a_licence_gate(monkeypatch):
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(),
                           live=app("Degraded", "CronJob has not completed its last execution successfully"))
    assert kinds(cand) == ["licence"]
    assert cand["gates"][0]["env"] == ENV and "Degraded" in cand["gates"][0]["why"]
    assert "not revoked yet" in text and f"Confirm-Licence-Revoked: {ENV}" in text
    assert "checked again" not in text, "a lifted gate is not checked again"
    assert all(len(line) <= 350 for line in text.splitlines() if "licence" in line.lower())


def test_purge_with_the_licence_revoked_passes(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=app(), lookups=calls)
    assert kinds(cand) == []
    assert "licence: revoked" in text
    assert calls == [f"{ENV}-ss"], "the supporting-services app is the one read"


@pytest.mark.parametrize("current,why", [(False, "does not show the commit that armed the purge"),
                                         (None, "could not tell")])
def test_a_green_cronjob_on_a_stale_live_state_is_not_a_revoke(monkeypatch, current, why):
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=app(), current=current)
    assert kinds(cand) == ["licence"] and why in cand["gates"][0]["why"]


def test_no_supporting_services_app_is_a_note(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, apps=[f"{ENV}-ms", f"{ENV}-glb"], lookups=calls)
    assert kinds(cand) == [] and calls == []
    assert "No supporting-services app" in text


def test_a_failed_lookup_is_a_licence_gate_that_retries(monkeypatch):
    cand, _ = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=None)
    assert kinds(cand) == ["licence"]
    assert cr.GATES["licence"][2] == "transient"


def test_a_chart_without_the_revoke_gets_a_note_and_no_gate(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(retire=False), live=app("Degraded"), lookups=calls)
    assert kinds(cand) == [] and calls == []
    assert "does not revoke the Constellation licence" in text


def test_a_chart_argo_cd_has_not_applied_yet_is_not_read_as_no_revoke(monkeypatch):
    # The render used Argo CD's chart. Main already asks for a newer one.
    monkeypatch.setitem(m._app_chart_revision_map, f"{ENV}-ss", "2604.2.0-rev3")
    monkeypatch.setattr(m, "_main_chart_revision", lambda a, sha: "2604.3.0-rev1")
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(retire=False), live=app("Degraded"))
    assert kinds(cand) == ["licence"]
    assert "2604.3.0-rev1" in cand["gates"][0]["why"] and "2604.2.0-rev3" in cand["gates"][0]["why"]
    monkeypatch.setattr(m, "_main_chart_revision", lambda a, sha: "2604.2.0-rev3")
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(retire=False), live=app("Degraded"))
    assert kinds(cand) == [] and "does not revoke" in text


def test_an_unrendered_supporting_services_app_is_never_proven_revoked(monkeypatch):
    # On a line without the revoke, a retiring CronJob with the monitor on also
    # ends green every minute, so the live state alone proves nothing.
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, ss_fails=True, live=app(), lookups=calls)
    assert kinds(cand) == ["licence"] and calls == []
    assert "did not render" in cand["gates"][0]["why"]
    assert f"Confirm-Licence-Revoked: {ENV}" in text


def test_decommission_alone_is_a_migration_and_reads_nothing(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, ARMED, ss=ss_resources(), live=None, lookups=calls)
    assert kinds(cand) == [] and calls == []
    assert "Constellation licence" not in text


def test_the_trailer_lifts_the_licence_gate():
    text, trailer, token, fix = cr.GATES["licence"]
    assert trailer == "Confirm-Licence-Revoked" and token == "transient" and not fix
    gate = {"kind": "licence", "env": ENV, "why": "x"}
    assert cr.gate_trailer(gate) == f"Confirm-Licence-Revoked: {ENV}"


# ── docs ─────────────────────────────────────────────────────────────────

def test_the_licence_gate_has_a_guards_row_and_an_internals_row():
    from test_cops2766_block3_docs import _cells, _guard_rows, _read
    rows = [r for r in _guard_rows() if _cells(r)[0] == "**Licence not revoked**"]
    assert len(rows) == 1 and "Confirm-Licence-Revoked: <env>" in _cells(rows[0])[3]
    assert "| Licence not revoked |" in _read("docs/internals.md")


def test_the_trailer_is_read_from_a_commit_message():
    import identity
    assert identity._confirmations([f"Retire it\n\nConfirm-Licence-Revoked: {ENV}"]) == \
        {f"confirm-licence-revoked: {ENV}"}


# ── the ArgoCD lookup ────────────────────────────────────────────────────

class _Run:
    def __init__(self, rc=0, out="", raise_=None):
        self.rc, self.out, self.raise_, self.args = rc, out, raise_, None

    def __call__(self, args, **kw):
        self.args = args
        if self.raise_:
            raise self.raise_
        return type("R", (), {"returncode": self.rc, "stdout": self.out, "stderr": "boom"})()


@pytest.mark.parametrize("run,want", [
    (_Run(0, '{"metadata": {"name": "x"}}'), {"metadata": {"name": "x"}}),
    (_Run(1, ""), None),
    (_Run(0, "null"), None),
    (_Run(0, "[]"), None),
    (_Run(0, "not json"), None),
    (_Run(raise_=m.subprocess.TimeoutExpired("argocd", 30)), None),
])
def test_the_lookup_returns_the_application_or_none(monkeypatch, run, want):
    monkeypatch.setattr(m.subprocess, "run", run)
    assert m._licence_app_json(f"argocd/{ENV}-ss") == want
    assert run.args[:4] == [m.ARGOCD_BIN, "app", "get", f"{ENV}-ss"]


# ── is the live state recent enough? ─────────────────────────────────────

GIT = {"repoURL": "git@ssh.bitbucket.org:appspace-cloud/acme-config-prod", "ref": "config"}
CHART = {"repoURL": "docker.repo.appspace.com", "chart": "appspace-supporting-services"}


def test_the_live_revision_is_the_one_of_the_git_source():
    multi = {"spec": {"sources": [CHART, GIT]}, "status": {"sync": {"revisions": ["2604.3.0", "abc"]}}}
    assert m._live_config_revision(multi) == "abc"
    multi["spec"]["sources"] = [GIT, CHART]
    assert m._live_config_revision(multi) == "2604.3.0"
    single = {"spec": {"source": GIT}, "status": {"sync": {"revision": "def"}}}
    assert m._live_config_revision(single) == "def"
    assert m._live_config_revision({"spec": {"sources": [CHART]}}) == ""
    assert m._live_config_revision(None) == ""


def _mirror(monkeypatch, last="armed1", ancestor_rc=0, has=True):
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", True)
    monkeypatch.setattr(m, "_mirror_disabled", False)
    monkeypatch.setattr(m, "_repo_for_sha", lambda sha: "acme-config-prod")
    monkeypatch.setattr(m, "_mirror_has_sha", lambda repo, sha: has)
    monkeypatch.setattr(m, "_mirror_path", lambda repo: "/mirror")
    seen = []

    def git(args, **kw):
        seen.append(args)
        if "log" in args:
            return type("R", (), {"returncode": 0, "stdout": last + "\n"})()
        return type("R", (), {"returncode": ancestor_rc, "stdout": ""})()
    monkeypatch.setattr(m, "_git_run", git)
    return seen


LIVE_APP = {"spec": {"sources": [GIT, CHART]}, "status": {"sync": {"revisions": ["live9", "2604.3.0"]}}}


def test_the_live_state_is_current_when_it_has_the_arming_commit(monkeypatch):
    seen = _mirror(monkeypatch)
    assert m._licence_live_is_current(LIVE_APP, "$config/" + IDENT, "main1") is True
    assert seen[0][-2:] == ["--", IDENT] and "main1" in seen[0]
    assert seen[1][-3:] == ["--is-ancestor", "armed1", "live9"]


@pytest.mark.parametrize("kw,want", [({"ancestor_rc": 1}, False), ({"ancestor_rc": 128}, None),
                                     ({"has": False}, None), ({"last": ""}, None)])
def test_the_live_state_is_stale_or_unknown(monkeypatch, kw, want):
    _mirror(monkeypatch, **kw)
    assert m._licence_live_is_current(LIVE_APP, IDENT, "main1") is want


def test_no_mirror_means_unknown(monkeypatch):
    _mirror(monkeypatch)
    monkeypatch.setattr(m, "GIT_MIRROR_ENABLED", False)
    assert m._licence_live_is_current(LIVE_APP, IDENT, "main1") is None
