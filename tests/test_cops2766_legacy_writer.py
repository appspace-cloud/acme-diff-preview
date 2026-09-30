"""COPS-2766 block 4, item 4: one key turns the legacy Helm writer back on.

ArgoCD owns the ms, ss and glb releases. The old ADO pipeline (helm_deploy.sh
in acme-components) still reads appspace.infra.deployGLB, deployMicroservices
and deploySupportingServices, and on the literal true it runs helm upgrade on
the same releases. No chart reads the keys, so the diff shows nothing.

Now a PR that sets one of them to true fails the build [blocked], unless a
commit message of the PR has `Confirm-LegacyHelm: <env>`. Then the diff runs,
and the merge summary shows the confirmed line. A true already on main, and
helmForceUpgrade, are not hits.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402
import identity  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, PATH_MAP, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML)

SK = ("acme-config-dev", 991)
MERGED = "feed0001aabb"
ERR = object()

PV = "gcp/prod/private-cloud/na1-a/monthly/pv-x-a"
ENV = f"{PV}/customer.yaml"
CICD = f"{PV}/cicd-versions.yaml"
COHORT = "gcp/prod/private-cloud/na1-a/monthly/config.yaml"
CL = "gcp/prod/public-cloud/na1-a/cl-prod-b"
AWS = "aws/prod/private-cloud/na1/monthly/pv-amzn-a/customer.yaml"
TRAILER = "Confirm-LegacyHelm: pv-orch-a"


def _infra(**kv):
    return "appspace:\n  infra:\n" + "".join(f"    {k}: {v}\n" for k, v in kv.items())


def _serve(monkeypatch, files):
    """_bb_fetch_status from `files[sha][path]`, ERR a failed read; returns the reads."""
    calls = []

    def fetch(path, sha, repo=None):
        calls.append((path, sha))
        v = files.get(sha, {}).get(path)
        if v is ERR:
            return None, m.BB_ERROR
        return (None, m.BB_NOT_FOUND) if v is None else (v, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    m._vf_cache.clear()     # a test may serve new bodies at the same sha
    return calls


def _hits(monkeypatch, head, base=None, changed=None, renames=None, base_sha="base1"):
    _serve(monkeypatch, {"head1": head, "base1": base or {}})
    hits = m._detect_legacy_writer_rearm(changed or sorted(head), renames or {}, "head1",
                                         base_sha, repo="acme-config-prod")
    return [(h["path"], h["key"], h["env"]) for h in hits]


# ── which change is a hit ────────────────────────────────────────────────

@pytest.mark.parametrize("key", m._LEGACY_WRITER_KEYS)
@pytest.mark.parametrize("value", ["true", '"true"', "True"])
def test_each_key_set_to_true_is_a_hit(monkeypatch, key, value):
    assert _hits(monkeypatch, {ENV: _infra(**{key: value})},
                 {ENV: _infra(**{key: "false"})}) == [(ENV, key, "pv-x-a")]


@pytest.mark.parametrize("head,base", [
    (_infra(deployGLB="true"), _infra(deployGLB="true")),        # already on main
    (_infra(deployGLB="false"), _infra(deployGLB="true")),       # turned off
    (_infra(deployGLB="false"), None),
    (_infra(deployWindows="true"), None),                        # not a writer key
    ("appspace:\n  helmForceUpgrade: true\n", None),             # only a note (S4)
    ("appspace:\n  infra: []\n", None),
    ("appspace: 3\n", None),
    ("- a\n", None),
    ("appspace: [\n", None),                                     # does not parse
])
def test_what_stays_quiet(monkeypatch, head, base):
    assert _hits(monkeypatch, {ENV: head}, {ENV: base} if base else None) == []


def test_a_deleted_file_is_skipped(monkeypatch):
    assert _hits(monkeypatch, {}, {ENV: _infra(deployGLB="true")}, changed=[ENV]) == []


@pytest.mark.parametrize("path,env", [
    (ENV, "pv-x-a"),
    (CICD, "pv-x-a"),
    (f"{PV}/extra.yaml", PV),
    (COHORT, "gcp/prod/private-cloud/na1-a/monthly"),
    ("gcp/config.yaml", "gcp"),
    ("gcp/prod/private-cloud/config.yaml", "gcp/prod/private-cloud"),
    (f"{CL}/config.yaml", "cl-prod-b"),
    (f"{CL}/app3/customer.yaml", "cl-prod-b"),
    (f"{CL}/constellation/cicd-versions.yaml", "cl-prod-b"),
    ("gcp/prod/public-cloud/na1-a/config.yml", "gcp/prod/public-cloud/na1-a"),
    ("azure/prod/private-cloud/na1-b/monthly/pv-y-a/customer.yaml", "pv-y-a"),
    (AWS, "pv-amzn-a"),
])
def test_every_value_file_under_a_cloud_folder_counts(monkeypatch, path, env):
    assert _hits(monkeypatch, {path: _infra(deployMicroservices="true")}) == [
        (path, "deployMicroservices", env)]


@pytest.mark.parametrize("path", [".ci/azure-pipelines-build.yaml", "acme-utils/x.yaml",
                                  "config.yaml", f"{PV}/notes.txt"])
def test_files_outside_the_cloud_folders_are_skipped(monkeypatch, path):
    assert _hits(monkeypatch, {path: _infra(deployGLB="true")}) == []


def test_dev_6509_adds_deploy_glb_to_an_app_folder(monkeypatch):
    """acme-config-dev #6509: removed 5 days later as deprecated."""
    app2 = "gcp/dev/public-cloud/ap1/cl-dev11-a/app2/customer.yaml"
    old = 'appspace:\n  instanceName: "cl-dev11-app2-a"\n'
    new = old + "  infra:\n    deployGLB: true\n    deployWindows: true\n"
    assert _hits(monkeypatch, {app2: new}, {app2: old}) == [(app2, "deployGLB", "cl-dev11-a")]


def test_a_new_file_with_true_is_a_hit(monkeypatch):
    assert _hits(monkeypatch, {ENV: _infra(deployGLB="true", deployMicroservices="true")}) == [
        (ENV, "deployGLB", "pv-x-a"), (ENV, "deployMicroservices", "pv-x-a")]


def test_a_move_compares_with_its_old_side(monkeypatch):
    old = "gcp/prod/private-cloud/na1-a/weekly/pv-x-a/customer.yaml"
    body = _infra(deployGLB="true")
    assert _hits(monkeypatch, {ENV: body}, {old: body}, changed=[old, ENV],
                 renames={old: ENV}) == []


def test_with_no_base_every_true_counts(monkeypatch):
    body = _infra(deployGLB="true")
    assert _hits(monkeypatch, {ENV: body}, {ENV: body}, base_sha="") == [
        (ENV, "deployGLB", "pv-x-a")]


def test_the_base_is_read_only_for_a_hit(monkeypatch):
    calls = _serve(monkeypatch, {"head1": {ENV: _infra(deployGLB="false")}})
    assert m._detect_legacy_writer_rearm([ENV], {}, "head1", "base1") == []
    assert calls == [(ENV, "head1")]


@pytest.mark.parametrize("side", ["head1", "base1"])
def test_a_failed_read_is_retried_never_passed(monkeypatch, side):
    files = {"head1": {ENV: _infra(deployGLB="true")}, "base1": {}}
    files[side][ENV] = ERR
    _serve(monkeypatch, files)
    with pytest.raises(m.ValueFileUnreadable):
        m._detect_legacy_writer_rearm([ENV], {}, "head1", "base1")


# ── the trailer and the gate ─────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("Confirm-LegacyHelm: pv-x", {"confirm-legacyhelm: pv-x"}),
    ("* `confirm-legacyhelm: PV-X`.", {"confirm-legacyhelm: pv-x"}),
    ("Confirm-LegacyHelm: gcp/prod/private-cloud/na1-a/monthly",
     {"confirm-legacyhelm: gcp/prod/private-cloud/na1-a/monthly"}),
    ("Confirm-Rename: a/pv-x-a → b/pv-x-a", {"confirm-rename: a/pv-x-a -> b/pv-x-a"}),
    ("Confirm-Legacy-Helm: pv-x", set()),
])
def test_confirmations_read_the_legacy_trailer(message, expected):
    assert identity._confirmations([message]) == expected


def test_the_gate_is_blocked_and_lifted_by_its_trailer():
    g = {"kind": "legacy_helm", "env": "pv-x-a"}
    assert cr.GATES["legacy_helm"] == ("The legacy Helm writer is switched back on",
                                       "Confirm-LegacyHelm", "blocked", "")
    assert cr.gate_trailer(g) == "Confirm-LegacyHelm: pv-x-a"
    assert cr.gate_trailer(g).lower() in identity._confirmations(["Confirm-LegacyHelm: PV-X-A"])


def test_merge_gates_keeps_every_gate_and_adds_the_extra_ones():
    ip = m.DiffResult("", [], 1, True, None, m.OUT_DIFF, "changes", ip_released=["x"])
    extra = [{"kind": "legacy_helm", "env": "pv-orch-a"}] * 2
    assert m._merge_gates(None, None, None, None, {"pv-orch-a-ss": ip}, extra) == [
        {"kind": "ip", "env": "pv-orch-a", "arg": "pv-orch-a", "lifted": False},
        {"kind": "legacy_helm", "env": "pv-orch-a", "arg": "pv-orch-a", "lifted": False}]


# ── the block ────────────────────────────────────────────────────────────

def _hit(path, key="deployGLB", env="pv-x-a"):
    return {"path": path, "key": key, "env": env}


def test_the_status_names_the_key_the_env_and_the_trailer():
    desc, body = m._legacy_writer_block([_hit(ENV)], PR_SHA, BASE_SHA)
    assert desc == ("BLOCKED: deployGLB: true in pv-x-a turns the legacy Helm writer back "
                    "on. To merge anyway, add Confirm-LegacyHelm: pv-x-a to a commit")
    assert m._extract_status_token(body) == "blocked"
    assert f"[base:{BASE_SHA[:8]}]" in body
    assert "⛔ **Blocked: this PR turns the legacy Helm writer back on.**" in body
    assert f"- `{ENV}`: `appspace.infra.deployGLB: true`" in body
    assert "`helm_deploy.sh`" in body and "\n- `Confirm-LegacyHelm: pv-x-a`\n" in body
    assert "—" not in body and "–" not in body


def test_each_env_gets_one_trailer_line():
    hits = [_hit(ENV), _hit(ENV, "deployMicroservices"),
            _hit("gcp/config.yaml", "deploySupportingServices", "gcp")]
    desc, body = m._legacy_writer_block(hits, PR_SHA, "")
    assert desc.startswith("BLOCKED: deployGLB: true in pv-x-a turns the legacy Helm "
                           "writer back on (+2 more). To merge anyway")
    assert body.count("\n- `Confirm-LegacyHelm: pv-x-a`\n") == 1
    assert "\n- `Confirm-LegacyHelm: gcp`\n" in body
    assert "[base:" not in body


def test_a_long_description_is_cut_and_keeps_the_trailer():
    env = "pv-" + "k" * 150
    desc, _ = m._legacy_writer_block([_hit(ENV, env=env)], PR_SHA, None)
    assert len(desc) == 255 and desc.endswith(f"add Confirm-LegacyHelm: {env} to a commit")
    env = "pv-" + "\u00e9" * 60
    desc, _ = m._legacy_writer_block([_hit(ENV, env=env)] * 2, PR_SHA, None)
    assert len(desc.encode()) <= 255 and desc.endswith(
        f"... (+1 more). To merge anyway, add Confirm-LegacyHelm: {env} to a commit")


# ── process_pr ───────────────────────────────────────────────────────────

ON = IDENTITY_YAML + "  infra:\n    deployGLB: true\n"


@pytest.fixture()
def legacy_pr(world, monkeypatch):
    """`files[sha][path]`, read at the merge preview; the PR changes pv-orch-a's
    customer.yaml. run(messages) stubs the commit messages: an exception is
    raised, None means they must not be read."""
    sinks, plan = world
    files = {BASE_SHA: {IDENTITY: IDENTITY_YAML}, MERGED: {IDENTITY: ON}}
    _serve(monkeypatch, files)
    monkeypatch.setattr(m, "_merge_preview", lambda repo, base, pr: (MERGED, []))
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([IDENTITY], {}))
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_retry_backoff", {})

    def run(messages):
        def commits(*a):
            if messages is None:
                pytest.fail("no gate, so no commit read")
            if isinstance(messages, Exception):
                raise messages
            return messages
        monkeypatch.setattr(m, "_pr_commit_messages", commits)
        m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1]
    return run, sinks, plan, files


def test_process_pr_blocks_deploy_glb_true(legacy_pr):
    run, sinks, _plan, _files = legacy_pr
    body, (state, desc) = run(["Turn the GLB back on"])
    assert state == "FAILED" and f"add {TRAILER} to a commit" in desc
    assert m._extract_status_token(body) == "blocked"
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert sinks.diff_calls == [], "blocked before any render"


def test_process_pr_the_trailer_lifts_it(legacy_pr):
    run, sinks, _plan, _files = legacy_pr
    body, (state, _desc) = run([f"Turn the GLB back on\n\n{TRAILER}\n"])
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert f"☑️ **Confirmed in a commit:** `{TRAILER}` ({cr.GATES['legacy_helm'][0]})" in body
    assert sorted(set(sinks.diff_calls)) == ["pv-orch-a-ms", "pv-orch-a-ss"]


def test_process_pr_a_trailer_for_another_env_does_not(legacy_pr):
    run, *_ = legacy_pr
    body, (state, _desc) = run(["Confirm-LegacyHelm: pv-orch-b"])
    assert state == "FAILED" and m._extract_status_token(body) == "blocked"


def test_process_pr_unreadable_commits_retry_and_never_lift(legacy_pr):
    run, *_ = legacy_pr
    body, (state, desc) = run(m.PrCommitsUnreadable("commit list is not ready"))
    assert state == "FAILED" and "will retry" in desc
    assert m._extract_status_token(body) == "transient"
    assert SK not in m._seen and SK in m._retry_backoff


def test_process_pr_with_no_hit_never_reads_the_commits(legacy_pr):
    run, _sinks, _plan, files = legacy_pr
    files[MERGED][IDENTITY] = IDENTITY_YAML + "  infra:\n    deployGLB: false\n"
    body, (state, _desc) = run(None)
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert cr.GATES["legacy_helm"][0] not in body


def test_process_pr_a_confirmed_hit_keeps_the_other_gates_open(legacy_pr):
    run, _sinks, plan, _files = legacy_pr
    plan["pv-orch-a-ss"] = m.DiffResult("", [], 1, True, None, m.OUT_DIFF, "changes",
                                        ip_released=["x"])
    body, (state, desc) = run([TRAILER])
    assert state == "FAILED" and m._extract_status_token(body) == "blocked"
    assert desc.startswith(f"Blocked - {cr.GATES['ip'][0]} in pv-orch-a.")
    assert f"☑️ **Confirmed in a commit:** `{TRAILER}`" in body
    assert f"⛔ **{cr.GATES['ip'][0]}** in `pv-orch-a`" in body


def test_a_confirmed_hit_on_a_new_env_shows_the_confirmed_line(world, monkeypatch):
    """The new-env-only path has a merge summary now (block 3), so it lists
    the lifted gate too."""
    import test_cops2766_new_env_gates as ne
    sinks, _plan = world
    files = dict(ne.FILES, **{ne.IDENT: ne.FILES[ne.IDENT] + "  infra:\n    deployGLB: true\n"})
    monkeypatch.setattr(m, "_bb_fetch_status", lambda path, sha, repo=None:
                        (files[path], m.BB_OK) if sha == PR_SHA and path in files
                        else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (ne.RENDERED, None, 3, "2603.0.1-dev"))
    monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: ([ne.IDENT], {}))
    monkeypatch.setattr(m, "_detect_new_env_candidates", lambda *a, **k: [dict(ne.CAND)])
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    trailer = f"Confirm-LegacyHelm: {ne.ENV}"
    monkeypatch.setattr(m, "_pr_commit_messages", lambda *a: [f"Add it\n\n{trailer}\n"])
    m.process_pr(_mk_pr(), PATH_MAP, base_sha=BASE_SHA)
    body, (state, desc) = sinks.upserts[-1], sinks.statuses[-1]
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert f"☑️ **Confirmed in a commit:** `{trailer}` ({cr.GATES['legacy_helm'][0]})" in body
    assert desc.startswith(f"⚠️ Confirmed in a commit: {trailer}"), desc
