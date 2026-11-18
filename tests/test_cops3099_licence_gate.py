"""COPS-3099: a private cloud retired for good revokes its Constellation licence.

With `decommission` and `decommissionPurgeData` armed, the appspace-client-admins
CronJob of the supporting-services app revokes the licence through
license-service, and its pass fails until that is done. Argo CD (3.5.2) shows
the CronJob Healthy with "CronJob has completed its last execution
successfully" once a pass succeeds after a failed one. It also shows Healthy,
with other messages, while a pass runs, before any pass succeeded and while the
CronJob is suspended, and right after the arming sync it can still show an old
invite pass. The check looks for that exact message, at least 3 minutes after
the last sync, on a cloud whose chart renders LICENCE_RETIRE.

It is a warning and never a gate (2.136.0). A QA test of 2.135.0 found the hub
shows the CronJob as running for most of every minute, so the answer flipped
between renders and the build went red again after it had cleared. The warning
asks for patience and gives the commands to check by hand.

A chart line without the revoke renders no LICENCE_RETIRE. Its folder removal
gets a note, so retirements on other release lines keep working.
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
            "spec": {"destination": {"name": "gcp-prod-pv-na2-a", "namespace": ENV}},
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


def test_purge_with_the_revoke_not_finished_is_a_warning_and_no_gate(monkeypatch):
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(),
                           live=app("Degraded", "CronJob has not completed its last execution successfully"))
    assert kinds(cand) == [], "the licence never blocks the PR"
    assert cr._DECOM_LICENCE_HDR in text
    assert "Degraded" in text and "the last pass failed" in text
    assert "does not block the PR" in text and "in about 5 minutes" in text and "be patient" in text
    assert "when main moves" in text and "pushed to the PR" in text, "says when the comment is refreshed"
    assert "an empty one is enough" in text and "armed long ago" in text and "about 3 minutes" in text
    assert "Replace `<spoke>`" not in text, "the cluster name is known here"


def test_the_warning_has_the_commands_to_check_by_hand(monkeypatch):
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(),
                           live=app(message="The job is running. Its last execution may not have been successful"))
    assert "ArgoCD says the job is running" in text and "says nothing about the licence" in text
    assert f"argocd app get argocd/{ENV}-ss -o json" in text
    assert f"kubectl --context gcp-prod-pv-na2-a -n {ENV} get pods" in text
    assert "grep -E 'licence\\.|Licence:'" in text
    for word in ("licence.revoked", "licence.waiting", "licence.failed", "Engineering",
                 "Licence: none for this host", "both decommission flags reached the cloud"):
        assert word in text
    assert "clears by itself" not in text and "so wait." not in text


def test_a_failed_lookup_has_no_cluster_name_and_says_so(monkeypatch):
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=None)
    assert kinds(cand) == [] and cr._DECOM_LICENCE_HDR in text
    assert "--context <spoke>" in text and "the ArgoCD lookup failed" in text
    assert "Replace `<spoke>` with the cluster of this environment" in text
    assert "appspace.infra.<cloud>.targetCluster" in text


@pytest.mark.parametrize("spec", [None, [], ["x"], {}, {"destination": None}, {"destination": []},
                                  {"destination": {"name": None}}, {"destination": {"server": "https://x"}}])
def test_a_odd_destination_in_the_application_never_breaks_the_comment(monkeypatch, spec):
    live = app("Degraded", "CronJob has not completed its last execution successfully")
    live["spec"] = spec
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=live)
    assert kinds(cand) == [] and cr._DECOM_LICENCE_HDR in text and "--context <spoke>" in text


def test_purge_with_the_licence_revoked_says_so(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=app(), lookups=calls)
    assert kinds(cand) == []
    assert "licence: revoked" in text and cr._DECOM_LICENCE_HDR not in text
    assert calls == [f"{ENV}-ss"], "the supporting-services app is the one read"


@pytest.mark.parametrize("current,why", [(False, "does not show the commit that armed the purge"),
                                         (None, "could not tell")])
def test_a_green_cronjob_on_a_stale_live_state_is_not_a_revoke(monkeypatch, current, why):
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=app(), current=current)
    assert kinds(cand) == [] and cr._DECOM_LICENCE_HDR in text and why in text


def test_no_supporting_services_app_is_a_note(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, apps=[f"{ENV}-ms", f"{ENV}-glb"], lookups=calls)
    assert kinds(cand) == [] and calls == []
    assert "No supporting-services app" in text


def test_a_chart_without_the_revoke_gets_a_note(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(retire=False), live=app("Degraded"), lookups=calls)
    assert kinds(cand) == [] and calls == []
    assert "does not revoke the Constellation licence" in text and cr._DECOM_LICENCE_HDR not in text


def test_a_chart_argo_cd_has_not_applied_yet_is_not_read_as_no_revoke(monkeypatch):
    # The render used Argo CD's chart. Main already asks for a newer one.
    monkeypatch.setitem(m._app_chart_revision_map, f"{ENV}-ss", "2604.2.0-rev3")
    monkeypatch.setattr(m, "_main_chart_revision", lambda a, sha: "2604.3.0-rev1")
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(retire=False), live=app("Degraded"))
    assert kinds(cand) == [] and cr._DECOM_LICENCE_HDR in text
    assert "2604.3.0-rev1" in text and "2604.2.0-rev3" in text
    assert "Check it by hand before you merge" in text, "not a timing problem, so no wait"
    assert "after that" not in text and "Check by hand (read-only):" in text, "no dangling wait"
    monkeypatch.setattr(m, "_main_chart_revision", lambda a, sha: "2604.2.0-rev3")
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(retire=False), live=app("Degraded"))
    assert kinds(cand) == [] and "does not revoke" in text and cr._DECOM_LICENCE_HDR not in text


def test_an_unrendered_supporting_services_app_is_never_proven_revoked(monkeypatch):
    # On a line without the revoke, a retiring CronJob with the monitor on also
    # ends green every minute, so the live state alone proves nothing.
    calls = []
    cand, text = _evaluate(monkeypatch, PURGE, ss_fails=True, live=app(), lookups=calls)
    assert kinds(cand) == [] and calls == []
    assert cr._DECOM_LICENCE_HDR in text and "did not render" in text
    assert "licence: revoked" not in text


def test_decommission_alone_is_a_migration_and_reads_nothing(monkeypatch):
    calls = []
    cand, text = _evaluate(monkeypatch, ARMED, ss=ss_resources(), live=None, lookups=calls)
    assert kinds(cand) == [] and calls == []
    assert "Constellation licence" not in text


def test_the_gate_and_its_trailer_are_gone():
    import identity
    assert "licence" not in cr.GATES
    assert identity._confirmations([f"Retire it\n\nConfirm-Licence-Revoked: {ENV}"]) == set()


def test_the_merge_summary_says_it_is_a_review_item_and_not_a_stop(monkeypatch):
    cand, text = _evaluate(monkeypatch, PURGE, ss=ss_resources(), live=app("Degraded"))
    summary = "\n".join(cr._build_merge_summary({}, {}, [], text.splitlines(), [], [], [], gates=[]))
    assert "licence is not confirmed revoked yet" in summary
    assert "this does not block the PR" in summary
    assert "in the decommission panel" in summary
    assert "5 minutes" not in summary, "the summary promises no wait: the panel says why and for how long"
    assert "DO NOT MERGE" not in summary


# ── docs ─────────────────────────────────────────────────────────────────

def test_the_docs_describe_the_warning_and_not_a_gate():
    from test_cops2766_block3_docs import _cells, _guard_rows, _read
    assert not [r for r in _guard_rows() if "icence" in _cells(r)[0]], "no guards row: it is no gate"
    text = " ".join((_read("docs/internals.md") + _read("docs/reference.md")).split())
    assert "Confirm-Licence-Revoked" not in text
    assert "is not confirmed revoked yet" in text and "never a gate" in text
    assert "never blocks the build" in text


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
