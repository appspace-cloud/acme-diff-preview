"""COPS-2766 block 5 S1: noCore changes get a panel, and one move fails.

`appspace.infra.noCore` changes much more than a load balancer: the URL map
default moves between the Windows Core VM and `<env>-bs-agw`, and about 109
Deployments restart. The comment said nothing about it: #4565 turned it on in
57 environments with a green, silent build, and #4684 gave 48 min of 502
because the Core VM was stopped too early (COPS-2758).

Now every flip of the effective value (the ancestor config.yaml chain plus
the env's customer.yaml, base against PR) is a REVIEW panel with the Core VM
rule, and the build stays green. One shape fails the build, with no trailer:
a move of a live env after which noCore is off only because the moved
customer.yaml does not set it. That is #4667 (a 5 h outage, the noCore
backends deleted), and a replay of 78 prod moves found no other hit.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import comment_render as cr  # noqa: E402
import diff_preview as m  # noqa: E402

from test_coverage_orchestration import (  # noqa: E402,F401
    world, _mk_pr, BASE_SHA, PR_SHA, IDENTITY, IDENTITY_YAML, ANCILLARY)

K = m._NOCORE_KEY
BASE, HEAD = "base0001", "head0001"
GB = "gcp/prod/private-cloud/gb1-b"
OLD = f"{GB}/weekly/pv-myschroders-a/customer.yaml"
NEW = f"{GB}/hardcoded/weekly/pv-myschroders-a/customer.yaml"
WEEKLY = f"{GB}/weekly/config.yaml"
HC_WEEKLY = f"{GB}/hardcoded/weekly/config.yaml"
ENV = "appspace:\n  customerName: myschroders\n  suffix: a\n  version: 2603.0.21\n"
ON = "appspace:\n  infra:\n    noCore: true\n"
OFF = "appspace:\n  infra:\n    noCore: false\n"
# The destination cohort of #4667 at 5100146b4: comments only, no appspace key.
COMMENT_ONLY = ("---\n# Hardcoded/frozen folder: the environments here pin "
                "their own chart version.\n# Nothing is set at this level.\n")
BAD = "appspace:\n  infra: [\n"
APPS = ["pv-myschroders-a-glb", "pv-myschroders-a-ms", "pv-myschroders-a-ss"]
DASHES = ("\u2014", "\u2013")


def _files(monkeypatch, files, errors=()):
    """files: {(path, sha): body}. Any other path is a 404, `errors` a 5xx."""
    with m._vf_cache_lock:
        m._vf_cache.clear()
    m._yaml_cache.clear()
    reads = []

    def fetch(path, sha, repo=None):
        reads.append((path, sha))
        if (path, sha) in errors:
            return None, m.BB_ERROR
        body = files.get((path, sha))
        return (body, m.BB_OK) if body is not None else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fetch)
    return reads


def _move_4667(moved_body=ENV):
    """#4667: gb1-b/weekly (noCore on) to gb1-b/hardcoded/weekly (comment only)."""
    return {(WEEKLY, BASE): ON, (OLD, BASE): ENV,
            (WEEKLY, HEAD): ON, (HC_WEEKLY, HEAD): COMMENT_ONLY, (NEW, HEAD): moved_body}


def _changes(changed, renames, path_map, base=BASE):
    return m._key_changes(changed, renames, path_map, HEAD, base, K)


def _change(**kw):
    c = {"env": "pv-x-a", "path": "gcp/prod/private-cloud/na1-a/weekly/pv-x-a/customer.yaml",
         "moved_from": None, "key": K, "old": False, "new": True, "pinned": False,
         "src": None, "value": None}
    c.update(kw)
    return c


# ── (a) _effective_key ───────────────────────────────────────────────────

def test_a_cohort_value_reaches_the_env(monkeypatch):
    _files(monkeypatch, {(WEEKLY, BASE): ON, (OLD, BASE): ENV})
    assert m._effective_key(OLD, BASE, K) == (True, WEEKLY, "ok")


def test_the_customer_yaml_wins(monkeypatch):
    _files(monkeypatch, {(WEEKLY, BASE): ON, (OLD, BASE): ENV + "  infra:\n    noCore: false\n"})
    assert m._effective_key(OLD, BASE, K) == (False, OLD, "ok")


def test_a_null_in_the_customer_yaml_is_unset(monkeypatch):
    """Helm drops a null key, so the chart default applies."""
    _files(monkeypatch, {(WEEKLY, BASE): ON, (OLD, BASE): ENV + "  infra:\n    noCore:\n"})
    assert m._effective_key(OLD, BASE, K) == (None, OLD, "ok")


def test_nothing_sets_it(monkeypatch):
    _files(monkeypatch, {(OLD, BASE): ENV})
    assert m._effective_key(OLD, BASE, K) == (None, None, "ok")


def test_a_comment_only_or_odd_file_reads_as_empty(monkeypatch):
    """#4667's destination cohort is '---' plus comments: the first doc is
    None. A list or a scalar reads as empty too, never a crash."""
    for odd in (COMMENT_ONLY, "- a\n- b\n", "just text\n", ""):
        _files(monkeypatch, {(WEEKLY, BASE): odd, (OLD, BASE): ENV})
        assert m._effective_key(OLD, BASE, K) == (None, None, "ok"), repr(odd)
    _files(monkeypatch, {(WEEKLY, BASE): ON, (OLD, BASE): "- a\n"})
    assert m._effective_key(OLD, BASE, K) == (True, WEEKLY, "ok")


def test_an_unparsable_file_is_unknown(monkeypatch):
    _files(monkeypatch, {(WEEKLY, BASE): BAD, (OLD, BASE): ENV})
    assert m._effective_key(OLD, BASE, K) == (None, None, "unparsable")
    _files(monkeypatch, {(OLD, BASE): BAD})
    assert m._effective_key(OLD, BASE, K) == (None, None, "unparsable")


def test_an_absent_identity_file_reads_nothing_else(monkeypatch):
    reads = _files(monkeypatch, {(WEEKLY, BASE): ON})
    assert m._effective_key(OLD, BASE, K) == (None, None, "absent")
    assert reads == [(OLD, BASE)]


def test_a_failed_read_raises_so_the_pr_retries(monkeypatch):
    _files(monkeypatch, {(OLD, BASE): ENV}, errors={(WEEKLY, BASE)})
    with pytest.raises(m.ValueFileUnreadable):
        m._effective_key(OLD, BASE, K)


def test_the_chain_is_read_root_first(monkeypatch):
    reads = _files(monkeypatch, {(OLD, BASE): ENV})
    m._effective_key(OLD, BASE, K)
    assert [p for p, _ in reads] == [
        OLD, "gcp/config.yaml", "gcp/prod/config.yaml",
        "gcp/prod/private-cloud/config.yaml", f"{GB}/config.yaml", WEEKLY]


def test_a_lenient_read_of_a_bad_date_still_counts(monkeypatch):
    """ArgoCD's Go YAML reads a bad date as text; so does this read."""
    _files(monkeypatch, {(OLD, BASE): ENV + "  when: 2026-02-30\n  infra:\n    noCore: true\n"})
    value, src, state = m._effective_key(OLD, BASE, K)
    assert (str(value).lower(), src, state) == ("true", OLD, "ok")


# ── (b) _key_changes ─────────────────────────────────────────────────────

def test_the_4667_move_turns_it_off(monkeypatch):
    _files(monkeypatch, _move_4667())
    assert _changes([OLD, NEW], {OLD: NEW}, {OLD: APPS}) == [{
        "env": "pv-myschroders-a", "path": NEW, "moved_from": OLD, "key": K,
        "old": True, "new": False, "pinned": False, "src": None, "value": None}]


def test_the_move_that_keeps_it_on_is_quiet(monkeypatch):
    _files(monkeypatch, _move_4667(ENV + "  infra:\n    noCore: true\n"))
    assert _changes([OLD, NEW], {OLD: NEW}, {OLD: APPS}) == []


def test_the_move_that_sets_it_false_is_pinned(monkeypatch):
    _files(monkeypatch, _move_4667(ENV + "  infra:\n    noCore: false\n"))
    [c] = _changes([OLD, NEW], {OLD: NEW}, {OLD: APPS})
    assert (c["old"], c["new"], c["pinned"], c["src"]) == (True, False, True, NEW)


def test_a_null_in_the_moved_file_is_not_pinned(monkeypatch):
    """Helm drops a null key, so noCore still ends up off."""
    _files(monkeypatch, _move_4667(ENV + "  infra:\n    noCore:\n"))
    [c] = _changes([OLD, NEW], {OLD: NEW}, {OLD: APPS})
    assert (c["old"], c["new"], c["pinned"], c["src"]) == (True, False, False, NEW)


def test_the_destination_cohort_can_decide(monkeypatch):
    files = _move_4667()
    files[(HC_WEEKLY, HEAD)] = OFF
    _files(monkeypatch, files)
    [c] = _changes([OLD, NEW], {OLD: NEW}, {OLD: APPS})
    assert (c["new"], c["pinned"], c["src"]) == (False, False, HC_WEEKLY)


def test_the_4684_move_into_nocore_is_an_on_flip(monkeypatch):
    _files(monkeypatch, {(OLD, BASE): ENV, (HC_WEEKLY, HEAD): ON, (NEW, HEAD): ENV})
    [c] = _changes([OLD, NEW], {OLD: NEW}, {OLD: APPS})
    assert (c["old"], c["new"], c["moved_from"]) == (False, True, OLD)
    assert m._nocore_gates([c]) == []


def test_a_move_of_an_env_that_is_not_live_is_skipped(monkeypatch):
    _files(monkeypatch, _move_4667())
    assert _changes([OLD, NEW], {OLD: NEW}, {}) == []


def _cohorts_4565():
    """#4565: two cohort config.yaml files gain noCore true."""
    a, b = f"{GB}/weekly", f"{GB}/monthly"
    kids = {f"{a}/pv-a-a/customer.yaml": ["pv-a-a-ms"],
            f"{a}/pv-b-a/customer.yaml": ["pv-b-a-ms", "pv-b-a-ss"],
            f"{b}/pv-c-a/customer.yaml": ["pv-c-a-ms"],
            f"{b}/pv-gone-a/customer.yaml": ["pv-gone-a-ms"]}
    path_map = {**kids, f"{a}/config.yaml": ["pv-a-a-ms", "pv-b-a-ms"],
                f"{b}/pv-c-a/cicd-versions.yaml": ["pv-c-a-ms"],
                "gcp/prod/private-cloud/na1-a/weekly/pv-other-a/customer.yaml": ["pv-other-a-ms"]}
    files = {}
    for p in kids:
        if "gone" not in p:
            files.update({(p, BASE): ENV, (p, HEAD): ENV})
    files[(f"{b}/pv-gone-a/customer.yaml", BASE)] = ENV
    files[(f"{b}/pv-new-a/customer.yaml", HEAD)] = ENV
    files.update({(f"{a}/config.yaml", BASE): "appspace: {}\n", (f"{a}/config.yaml", HEAD): ON,
                  (f"{b}/config.yaml", HEAD): ON})
    changed = [f"{a}/config.yaml", f"{b}/config.yaml", f"{b}/pv-gone-a/customer.yaml",
               f"{b}/pv-new-a/customer.yaml"]
    return files, changed, path_map


def test_a_cohort_flip_reaches_every_live_env_below_it(monkeypatch):
    """A kid deleted in the PR (a teardown) and a brand-new env are skipped,
    and so is an env under another cohort."""
    files, changed, path_map = _cohorts_4565()
    _files(monkeypatch, files)
    out = _changes(changed, {}, path_map)
    assert [(c["env"], c["old"], c["new"], c["moved_from"]) for c in out] == [
        ("pv-c-a", False, True, None), ("pv-a-a", False, True, None),
        ("pv-b-a", False, True, None)]


def test_a_cohort_edit_that_leaves_the_key_alone_reads_no_chain(monkeypatch):
    files, changed, path_map = _cohorts_4565()
    files = {k: v.replace("noCore: true", "other: 1") for k, v in files.items()}
    _files(monkeypatch, files)
    monkeypatch.setattr(m, "_effective_key", lambda *a, **k: pytest.fail("no chain read"))
    assert _changes(changed[:2], {}, path_map) == []


def test_a_version_bump_reads_no_chain(monkeypatch):
    _files(monkeypatch, {(OLD, BASE): ENV, (OLD, HEAD): ENV.replace("2603.0.21", "2603.0.22")})
    monkeypatch.setattr(m, "_effective_key", lambda *a, **k: pytest.fail("no chain read"))
    assert _changes([OLD, "/" + OLD, f"{GB}/weekly/pv-myschroders-a/cicd-versions.yaml"],
                    {}, {OLD: APPS}) == []


def test_an_env_edit_that_flips_it(monkeypatch):
    _files(monkeypatch, {(OLD, BASE): ENV, (OLD, HEAD): ENV + "  infra:\n    noCore: true\n"})
    [c] = _changes([OLD], {}, {OLD: APPS})
    assert (c["env"], c["old"], c["new"], c["pinned"], c["src"]) == (
        "pv-myschroders-a", False, True, True, OLD)


def test_an_env_that_is_not_on_main_is_skipped(monkeypatch):
    """Its apps are still in ArgoCD (a leftover), and the PR adds the file
    back: there is no base value to compare with."""
    _files(monkeypatch, {(OLD, HEAD): ENV + "  infra:\n    noCore: true\n"})
    assert _changes([OLD], {}, {OLD: APPS}) == []


def test_an_edit_of_an_env_that_is_not_live_is_skipped(monkeypatch):
    _files(monkeypatch, {(OLD, BASE): ENV, (OLD, HEAD): ENV + "  infra:\n    noCore: true\n"})
    assert _changes([OLD], {}, {}) == []


def test_without_a_base_there_is_nothing_to_compare(monkeypatch):
    _files(monkeypatch, _move_4667())
    assert _changes([OLD, NEW], {OLD: NEW}, {OLD: APPS}, base="") == []


def test_an_unparsable_side_is_kept_as_unknown(monkeypatch):
    _files(monkeypatch, {(OLD, BASE): ENV, (OLD, HEAD): BAD})
    [c] = _changes([OLD], {}, {OLD: APPS})
    assert (c["old"], c["new"]) == (False, None)


def test_a_renamed_cohort_file_is_left_to_its_moved_envs(monkeypatch):
    """The cohort moves with its envs: each env is a rename pair."""
    new_cohort = f"{GB}/weekly2/config.yaml"
    new_env = f"{GB}/weekly2/pv-myschroders-a/customer.yaml"
    _files(monkeypatch, {(WEEKLY, BASE): ON, (OLD, BASE): ENV,
                         (new_cohort, HEAD): ON, (new_env, HEAD): ENV})
    assert _changes([WEEKLY, new_cohort, OLD, new_env],
                    {WEEKLY: new_cohort, OLD: new_env}, {OLD: APPS, WEEKLY: APPS}) == []


PUB = "gcp/prod/public-cloud/na1-a"


def test_public_cloud_envs_are_named_from_their_apps(monkeypatch):
    """A block is `cl-prod-b-app1`, not `app1`, and two constellations that
    both have an app1 stay apart."""
    path_map = {f"{PUB}/cl-prod-b/constellation/customer.yaml": ["cl-prod-b-ms", "cl-prod-b-ss"],
                f"{PUB}/cl-prod-b/app1/customer.yaml": ["cl-prod-b-app1-glb"],
                f"{PUB}/cl-pre-a/app1/customer.yaml": ["argocd-x/cl-pre-a-app1-glb"],
                f"{PUB}/cl-pre-a/config.yaml": ["cl-pre-a-app1-glb"]}
    files = {(f"{PUB}/{cl}/config.yaml", sha): ON if sha == BASE else "appspace: {}\n"
             for cl in ("cl-prod-b", "cl-pre-a") for sha in (BASE, HEAD)}
    for p in path_map:
        if p.endswith("customer.yaml"):
            files.update({(p, BASE): ENV, (p, HEAD): ENV})
    _files(monkeypatch, files)
    out = _changes([f"{PUB}/cl-prod-b/config.yaml", f"{PUB}/cl-pre-a/config.yaml"], {}, path_map)
    assert sorted(c["env"] for c in out) == ["cl-pre-a-app1", "cl-prod-b", "cl-prod-b-app1"]
    assert {(c["old"], c["new"]) for c in out} == {(True, False)}
    assert m._nocore_gates(out) == [], "a cohort flip is never the gate"


# ── (c) _nocore_gates ────────────────────────────────────────────────────

def test_only_an_unpinned_move_to_off_is_a_gate():
    moved = dict(moved_from=OLD, old=True, new=False, env="pv-m-a")
    assert m._nocore_gates([_change(**moved)]) == [
        {"kind": "nocore_lost", "env": "pv-m-a", "arg": "pv-m-a", "lifted": False}]
    for other in (dict(moved, pinned=True), dict(moved, moved_from=None),
                  dict(moved, old=False, new=True), dict(moved, new=None),
                  dict(moved, key="appspace.infra.legacyBackends")):
        assert m._nocore_gates([_change(**other)]) == [], other
    assert m._nocore_gates(None) == [] and m._nocore_gates([]) == []


# ── (d) _nocore_flip_lines ───────────────────────────────────────────────

def _text(changes):
    return "\n".join(m._nocore_flip_lines(changes))


def test_no_change_no_panel():
    assert m._nocore_flip_lines([]) == []


def test_a_crash_says_so():
    out = _text(None)
    assert "noCore check unavailable for this PR (see the service log)" in out
    assert "###" not in out


def test_the_on_text_carries_the_core_vm_rule():
    out = _text([_change()])
    assert out.startswith("### \U0001f50c noCore changes in 1 environment(s): on in 1, off in 0")
    assert ("⚠️ **noCore changes.** `appspace.infra.noCore` goes from `false` "
            "to `true` in 1 environment(s): `pv-x-a`.") in out
    assert "`<env>-bs-agw`" in out and "30 to 100 minutes (COPS-2758)" in out
    assert "#4684" in out
    assert "nocore-cutover" not in out and "acme-components" not in out
    assert "⛔" not in out


def test_the_off_text_names_the_deleted_backends():
    out = _text([_change(old=True, new=False)])
    assert "on in 0, off in 1" in out and "goes from `true` to `false`" in out
    assert "`bs-pcs`, `hc-pcs`" in out and "#4667" in out
    assert "⛔" not in out, "no gate, no stop sign"


def test_azure_gets_the_nginx_frontend_text():
    az = "azure/prod/private-cloud/na1-a/weekly/pv-az-a/customer.yaml"
    on, off = _text([_change(path=az, env="pv-az-a")]), _text(
        [_change(path=az, env="pv-az-a", old=True, new=False)])
    assert "nginx-frontend stops proxying to the Windows Core VM" in on
    assert "nginx-frontend proxies to the Windows Core VM again" in off
    assert "URL map" not in on + off and "bs-pcs" not in off


def test_one_panel_can_mix_clouds_and_directions():
    az = "azure/prod/private-cloud/na1-a/weekly/pv-az-a/customer.yaml"
    out = m._nocore_flip_lines([_change(), _change(path=az, env="pv-az-a"),
                                _change(env="pv-y-a", old=True, new=False)])
    assert out[0] == "### \U0001f50c noCore changes in 3 environment(s): on in 2, off in 1"
    assert sum(1 for l in out if l.startswith("⚠️ **noCore changes.**")) == 3


def test_the_gated_env_gets_the_fix_line():
    gated = _change(env="pv-myschroders-a", path=NEW, moved_from=OLD, old=True, new=False)
    out = _text([gated, _change(env="pv-y-a", old=True, new=False)])
    assert "in 2 environment(s): `pv-myschroders-a` (moved from `gcp/prod/private-cloud/gb1-b/weekly`)" in out
    assert (f"⛔ `{GB}/weekly/pv-myschroders-a` moves to "
            f"`{GB}/hardcoded/weekly/pv-myschroders-a`, and noCore turns off only because "
            "the new folder does not set it. Set `appspace.infra.noCore` in the moved "
            "`customer.yaml`: `true` to keep noCore, or `false` if Core must come back. "
            "The build fails until the file sets it.") in out
    assert out.count("⛔") == 1


@pytest.mark.parametrize("src,value,why", [
    (HC_WEEKLY, False, f"because `{HC_WEEKLY}` sets it to `false`"),
    (NEW, None, f"because `{NEW}` sets it to `null`"),
])
def test_the_fix_line_names_the_file_that_decides(src, value, why):
    out = _text([_change(env="pv-m-a", path=NEW, moved_from=OLD, old=True, new=False,
                         src=src, value=value)])
    assert f"and noCore turns off {why}. Set `appspace.infra.noCore`" in out, out


def test_a_pinned_or_on_move_has_no_fix_line():
    for c in (_change(path=NEW, moved_from=OLD, old=True, new=False, pinned=True, src=NEW),
              _change(path=NEW, moved_from=OLD)):
        assert "⛔" not in _text([c])


def test_an_unknown_env_is_named():
    out = m._nocore_flip_lines([_change(new=None), _change(env="pv-y-a")])
    assert out[0].endswith("in 1 environment(s): on in 1, off in 0")
    assert ("⚠️ noCore check unavailable for `pv-x-a`: one of its value "
            "files is not valid YAML.") in out
    only = _text([_change(old=None)])
    assert "###" not in only and "check unavailable for `pv-x-a`" in only


def test_names_are_capped_at_ten():
    out = _text([_change(env=f"pv-e{i:02}-a") for i in range(13)])
    assert "`pv-e09-a` (+3 more)." in out and "pv-e10-a" not in out
    unknown = _text([_change(env=f"pv-e{i:02}-a", new=None) for i in range(12)])
    assert "`pv-e09-a` (+2 more): one of their value files" in unknown


def test_no_em_or_en_dash_in_any_line():
    gated = _change(env="pv-m-a", path=NEW, moved_from=OLD, old=True, new=False)
    az = "azure/prod/private-cloud/na1-a/weekly/pv-az-a/customer.yaml"
    for changes in (None, [gated, _change(), _change(new=None), _change(path=az),
                           _change(path=az, old=True, new=False)]):
        out = _text(changes)
        assert not any(d in out for d in DASHES), out


# ── (e) the merge summary ────────────────────────────────────────────────

def _bullets(state, gates=None, results=None):
    lines = cr._build_merge_summary(results or {}, {}, None, None, state, None, False,
                                    gates=gates)
    return lines[2], [l[2:] for l in lines if l.startswith("- ")]


def test_a_flip_is_a_review_that_leads_the_deletions():
    results = {"pv-x-a-glb": m.DiffResult(
        "d", [], 2, True, "", m.OUT_DIFF, "", None,
        ["/compute.cnrm.cloud.google.com/ComputeBackendService pv-x-a/pv-x-a-bs-pcs",
         "/compute.cnrm.cloud.google.com/ComputeHealthCheck pv-x-a/pv-x-a-hc-pcs"])}
    verdict, b = _bullets(m._nocore_flip_lines([_change(old=True, new=False)]),
                          results=results)
    assert verdict.startswith("⚠️ **Review before merging** (2 item(s))")
    assert b[0] == ("\U0001f50c **noCore changes in 1 environment(s)**: on in 0, off in 1 "
                    "(read the noCore note, COPS-2758)")
    assert b[1].startswith("\U0001f5d1️ **2 resource(s) deleted**")
    lead = cr.status_lead("\n".join(cr._build_merge_summary(
        results, {}, None, None, m._nocore_flip_lines([_change()]), None, False)))
    assert lead.startswith("⚠️ noCore changes in 1 environment(s): on in 1"), lead


def test_the_gate_is_do_not_merge():
    gated = _change(env="pv-m-a", path=NEW, moved_from=OLD, old=True, new=False)
    verdict, b = _bullets(m._nocore_flip_lines([gated]), gates=m._nocore_gates([gated]))
    assert verdict.startswith("⛔ **DO NOT MERGE**")
    assert b[0] == "⛔ **A move turns noCore off** in `pv-m-a`"
    assert b[1].startswith("\U0001f50c **noCore changes in 1")


def test_an_unknown_is_a_review_not_a_routine():
    for state in (m._nocore_flip_lines(None), m._nocore_flip_lines([_change(new=None)])):
        verdict, b = _bullets(state)
        assert verdict.startswith("⚠️ **Review before merging** (1 item(s))")
        assert b == ["\U0001f50c **noCore check unavailable** for part of this PR "
                     "(see the noCore note)"]


def test_the_gate_table_has_the_kind():
    assert cr.GATES["nocore_lost"] == ("A move turns noCore off", None, "blocked")
    assert cr.gate_status_description(m._nocore_gates([_change(
        env="pv-m-a", moved_from=OLD, old=True, new=False)])) == \
        "Blocked - A move turns noCore off in pv-m-a (see PR comment)"


# ── (f) process_pr ───────────────────────────────────────────────────────

SK = ("acme-config-dev", 991)
CUSTOM = "gcp/dev/private-cloud/ap1/custom"
MONTHLY = "gcp/dev/private-cloud/ap1/monthly"
MOVED = f"{MONTHLY}/pv-orch-a/customer.yaml"
ORCH_APPS = ["pv-orch-a-ms", "pv-orch-a-ss"]
PMAP = {IDENTITY: ORCH_APPS, ANCILLARY: ORCH_APPS, f"{CUSTOM}/config.yaml": ORCH_APPS}
GATED_DESC = "Blocked - A move turns noCore off in pv-orch-a (see PR comment)"


@pytest.fixture()
def move(world, monkeypatch):
    """pv-orch-a moves from custom (noCore on) to monthly (nothing set)."""
    sinks, _plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_pr_commit_messages",
                        lambda *a: pytest.fail("the noCore gate has no trailer"))

    def run(moved_body=IDENTITY_YAML, changed=None, renames=None, errors=(), files=None):
        world_files = files or {
            (f"{CUSTOM}/config.yaml", BASE_SHA): ON, (f"{CUSTOM}/config.yaml", PR_SHA): ON,
            (IDENTITY, BASE_SHA): IDENTITY_YAML,
            (f"{MONTHLY}/config.yaml", PR_SHA): COMMENT_ONLY, (MOVED, PR_SHA): moved_body}
        reads = _files(monkeypatch, world_files, errors)
        monkeypatch.setattr(m, "get_pr_changed_files", lambda pr_id, repo=None: (
            changed or [IDENTITY, MOVED], {IDENTITY: MOVED} if renames is None else renames))
        m.process_pr(_mk_pr(), PMAP, base_sha=BASE_SHA)
        return sinks.upserts[-1], sinks.statuses[-1], reads
    return run


def test_the_4667_shape_fails_the_build(move):
    body, (state, desc), _reads = move()
    assert (state, desc) == ("FAILED", GATED_DESC)
    assert m._extract_status_token(body) == "blocked"
    assert "- ⛔ **A move turns noCore off** in `pv-orch-a`" in body
    assert "### \U0001f50c noCore changes in 1 environment(s): on in 0, off in 1" in body
    assert "noCore turns off only because the new folder does not set it" in body
    assert m._seen.get(SK) == (PR_SHA, BASE_SHA)
    assert SK not in m._retry_backoff


def test_setting_it_in_the_moved_file_lifts_it(move):
    body, (state, desc), _reads = move(IDENTITY_YAML + "  infra:\n    noCore: false\n")
    assert state == "SUCCESSFUL", desc
    assert desc.startswith("⚠️ noCore changes in 1 environment(s)"), desc
    assert m._extract_status_token(body) == "clean"
    assert "⛔" not in body.split("\n---\n")[0]


def test_keeping_it_on_has_no_panel(move):
    body, (state, _desc), _reads = move(IDENTITY_YAML + "  infra:\n    noCore: true\n")
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean"
    assert "noCore" not in body


def test_a_cohort_flip_stays_green(move):
    files = {(f"{CUSTOM}/config.yaml", BASE_SHA): ON, (f"{CUSTOM}/config.yaml", PR_SHA): OFF,
             (IDENTITY, BASE_SHA): IDENTITY_YAML, (IDENTITY, PR_SHA): IDENTITY_YAML}
    body, (state, desc), _reads = move(changed=[f"{CUSTOM}/config.yaml"], renames={},
                                       files=files)
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean", desc
    assert desc.startswith("⚠️ noCore changes in 1 environment(s): on in 0, off in 1")
    assert "⛔" not in body.split("\n---\n")[0]


def test_a_failed_chain_read_retries(move, monkeypatch):
    def unreadable(*a, **k):
        raise m.ValueFileUnreadable("value file unreadable: gcp/dev/config.yaml")
    monkeypatch.setattr(m, "_effective_key", unreadable)
    body, (state, _desc), _reads = move()
    assert m._extract_status_token(body) == "transient" and state == "FAILED"
    assert SK not in m._seen and SK in m._retry_backoff


def test_a_bug_in_the_check_is_a_review_line(move, monkeypatch):
    monkeypatch.setattr(m, "_key_changes", lambda *a, **k: 1 / 0)
    body, (state, desc), _reads = move()
    assert state == "SUCCESSFUL" and m._extract_status_token(body) == "clean", desc
    assert "noCore check unavailable for this PR (see the service log)" in body
    assert desc.startswith("⚠️ noCore check unavailable"), desc


def test_a_pr_without_config_changes_reads_nothing_for_it(world, monkeypatch):
    """The world default changes only cicd-versions.yaml: no noCore read."""
    sinks, _plan = world
    monkeypatch.setattr(m, "generate_ai_summary", lambda app_results: None)
    monkeypatch.setattr(m, "_effective_key", lambda *a, **k: pytest.fail("no chain read"))
    m.process_pr(_mk_pr(), PMAP, base_sha=BASE_SHA)
    assert m._extract_status_token(sinks.upserts[-1]) == "clean"
    assert sinks.statuses[-1][0] == "SUCCESSFUL"
