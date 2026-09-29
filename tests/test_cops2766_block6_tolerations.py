"""COPS-2766 block 6, item 2: a toleration the API rejects fails the build.

helm renders any string as a toleration `operator` or `effect`. The API only
takes the Kubernetes enum, case-sensitive, so acme-config-prod #4681
(`operator: equal`) rendered fine, failed on sync and froze the app. The check
reads the rendered PR docs that are new or changed, and reports only the bad
values that main does not already have. It reuses REASON_SCHEMA_INVALID, so it
is permanent: no retry, token [permanent], FAILED, no trailer.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import schema_errors  # noqa: E402
from schema_errors import _toleration_errors  # noqa: E402

KEY = ("apps/Deployment", "pv-statestreet-c", "appspace-mediatransform")
HEADER = "Kubernetes rejects these tolerations:"
OP_LINE = ("- at `Deployment appspace-mediatransform`: the Kubernetes API "
           "rejects toleration operator `equal`, want `Equal` or `Exists`")


def _deploy(tolerations, image="img:1", name="appspace-mediatransform"):
    """A rendered Deployment, helm style, with `tolerations` under the pod spec."""
    return (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  namespace: pv-statestreet-c\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: app\n"
        f"          image: {image}\n"
        f"{tolerations}"
        "      affinity:\n"
        "        nodeAffinity:\n"
        "          requiredDuringSchedulingIgnoredDuringExecution:\n"
        "            nodeSelectorTerms:\n"
        "              - matchExpressions:\n"
        "                  - key: gke_node_type\n"
        "                    operator: In\n"
        "                    values:\n"
        "                      - standard\n"
    )


def _tol(operator="Equal", effect="NoSchedule"):
    return (
        "      tolerations:\n"
        f"        - effect: {effect}\n"
        "          key: gke_node_type\n"
        f"          operator: {operator}\n"
        "          value: standard-high-resource\n"
    )


def _errors(text):
    return _toleration_errors({KEY: text})


# ── (a) the scanner ──────────────────────────────────────────────────────

def test_enum_values_and_empty_are_accepted():
    for op in ("Equal", "Exists", '""', "''", "", '"Equal"', "'Exists'"):
        assert _errors(_deploy(_tol(operator=op))) == "", op
    for eff in ("NoSchedule", "PreferNoSchedule", "NoExecute", '""', ""):
        assert _errors(_deploy(_tol(effect=eff))) == "", eff


def test_null_and_tilde_are_empty_but_a_quoted_null_is_a_string():
    assert _errors(_deploy(_tol(operator="null"))) == ""
    assert _errors(_deploy(_tol(effect="~"))) == ""
    assert _errors(_deploy(_tol(operator="# no value, just a comment"))) == ""
    out = _errors(_deploy(_tol(operator='"null"')))
    assert "toleration operator `null`" in out


def test_wrong_case_operator_is_flagged():
    out = _errors(_deploy(_tol(operator="equal")))
    assert out == HEADER + "\n" + OP_LINE


def test_wrong_case_effect_is_flagged():
    out = _errors(_deploy(_tol(effect="noschedule")))
    assert out.splitlines()[1] == (
        "- at `Deployment appspace-mediatransform`: the Kubernetes API "
        "rejects toleration effect `noschedule`, want `NoSchedule`, "
        "`PreferNoSchedule` or `NoExecute`")


def test_affinity_operator_outside_the_block_is_not_read():
    # _deploy puts `operator: In` under affinity, after the block closes.
    assert _errors(_deploy(_tol())) == ""
    assert _errors(_deploy("")) == ""


def test_a_sibling_key_closes_the_block():
    text = (
        "      tolerations:\n"
        "        - key: a\n"
        "          operator: Exists\n"
        "      nodeSelector:\n"
        "        operator: In\n"
    )
    assert _errors(text) == ""


def test_a_key_after_a_list_dash_closes_on_its_sibling_keys():
    # `- tolerations:` sits in a list item. Its sibling keys are at the key
    # column, not the dash column, and must close the block.
    text = (
        "    - tolerations:\n"
        "        - key: a\n"
        "          operator: Exists\n"
        "      affinity:\n"
        "        nodeAffinity:\n"
        "          - operator: In\n"
    )
    assert _errors(text) == ""
    assert "operator `equal`" in _errors(text.replace("Exists", "equal"))


def test_items_at_the_same_indent_as_the_key_are_read():
    text = (
        "      tolerations:\n"
        "      - key: a\n"
        "        operator: equal\n"
        "      - operator: Exists\n"
        "        effect: noexecute\n"
        "      nodeSelector: {}\n"
    )
    out = _errors(text).splitlines()
    assert out[1] == OP_LINE
    assert "toleration effect `noexecute`" in out[2]
    assert len(out) == 3


def test_an_inline_comment_is_stripped():
    assert _errors(_deploy(_tol(operator="Equal  # the default"))) == ""
    assert _errors(_deploy(_tol(operator='"Exists" # quoted'))) == ""
    out = _errors(_deploy(_tol(operator="equal # typo")))
    assert "toleration operator `equal`," in out


def test_blank_and_comment_lines_do_not_close_the_block():
    text = (
        "      tolerations:\n"
        "\n"
        "# a comment at column 0\n"
        "        - key: a\n"
        "          operator: equal\n"
    )
    assert _errors(text) == HEADER + "\n" + OP_LINE


def test_a_flow_empty_list_never_opens_a_block():
    text = (
        "      tolerations: []\n"
        "        operator: equal\n"
    )
    assert _errors(text) == ""


def test_a_repeated_bad_value_in_one_doc_is_listed_once():
    text = _tol(operator="equal") + _tol(operator="equal").replace(
        "      tolerations:\n", "")
    assert _errors(text) == HEADER + "\n" + OP_LINE


def test_five_bad_values_show_three_lines_and_the_rest_as_a_count():
    docs = {("apps/Deployment", "pv-x", f"svc-{i}"): _deploy(
        _tol(operator=f"bad{i}"), name=f"svc-{i}") for i in range(5)}
    out = _toleration_errors(docs).splitlines()
    assert out[0] == HEADER
    assert [l.split("`")[1] for l in out[1:4]] == [
        "Deployment svc-0", "Deployment svc-1", "Deployment svc-2"]
    assert out[4] == "- ... and 2 more"
    assert len(out) == 5


def test_long_names_never_get_a_line_cut_by_the_400_char_limit():
    docs = {}
    for i in range(5):
        name = f"appspace-elasticsearch-master-long-name-{i}"
        docs[("apps/StatefulSet", "pv-x", name)] = _deploy(
            _tol(effect="noschedule"), name=name)
    out = _toleration_errors(docs)
    stored = m._redact_error_detail(out)[:400]
    assert stored == out, "the stored detail must not be cut"
    lines = out.splitlines()
    assert all(l.endswith("`NoExecute`") for l in lines[1:-1])
    assert lines[-1] == f"- ... and {5 - (len(lines) - 2)} more"
    assert len(lines) - 2 >= 1


def test_a_very_long_first_line_is_still_shown():
    name = "x" * 400
    docs = {("apps/Deployment", "pv-x", name): _deploy(
                _tol(operator="equal"), name=name),
            ("apps/Deployment", "pv-x", "y"): _deploy(
                _tol(operator="equal"), name="y")}
    lines = _toleration_errors(docs).splitlines()
    assert lines[1].startswith("- at `Deployment xxx")
    assert lines[2] == "- ... and 1 more"


def test_empty_input_gives_empty():
    assert _toleration_errors({}) == ""
    assert _toleration_errors({}, {}) == ""


def test_a_bad_value_main_already_has_is_not_reported():
    main = {KEY: _deploy(_tol(operator="equal"), image="img:1")}
    pr = {KEY: _deploy(_tol(operator="equal"), image="img:2")}
    assert _toleration_errors(pr, main) == "", \
        "an old bad value must not block an unrelated PR"
    assert _toleration_errors(main, main) == ""
    pr2 = {KEY: _deploy(_tol(operator="equal", effect="noschedule"))}
    out = _toleration_errors(pr2, main).splitlines()
    assert len(out) == 2 and "toleration effect `noschedule`" in out[1]


def test_a_new_resource_is_read_in_full():
    assert _toleration_errors({KEY: _deploy(_tol(operator="equal"))},
                              {}) == HEADER + "\n" + OP_LINE


def test_the_hub_re_exports_it():
    assert m._toleration_errors is schema_errors._toleration_errors


# ── (b) _run_one_diff with a stubbed chart, values and helm ──────────────

APP = "pv-statestreet-c-ms"
PR_SHA, MAIN_SHA = "prsha0000001", "mainsha00001"


def _world(monkeypatch, pr_doc, main_doc):
    monkeypatch.setitem(m._app_chart_map, APP, "appspace-micro-services")
    monkeypatch.setitem(m._app_chart_revision_map, APP, "2604.0.1")
    monkeypatch.setitem(m._app_chart_registry_map, APP, "registry.example.com")
    monkeypatch.setitem(m._app_value_files_map, APP,
                        ["$config/gcp/prod/pv-statestreet-c/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, APP, "pv-statestreet-c")
    monkeypatch.setattr(m, "_ensure_chart", lambda reg, chart, ver: "/fake/chart")
    monkeypatch.setattr(m, "_fetch_value_files", lambda vfs, sha: {
        vf: f"side: {'pr' if sha == PR_SHA else 'main'}\n" for vf in vfs})
    monkeypatch.setattr(m, "_main_render_content_key", lambda *a: "k-tol")
    monkeypatch.setattr(m, "_main_render_cache_get", lambda k: (None, None, "miss"))
    monkeypatch.setattr(m, "_main_render_cache_put", lambda *a: None)
    calls = []

    def fake_template(chart, release, namespace, vals):
        calls.append(release)
        pr = "side: pr" in "".join(vals.values())
        return (pr_doc if pr else main_doc), None
    monkeypatch.setattr(m, "_helm_template", fake_template)
    return calls


def test_run_one_diff_fails_on_a_new_bad_operator(monkeypatch):
    _world(monkeypatch, _deploy(_tol(operator="equal")), _deploy(_tol()))
    diff_text, reason, detail = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert diff_text is None and reason == m.REASON_SCHEMA_INVALID
    assert "`equal`" in detail and detail.startswith(HEADER)


def test_run_one_diff_passes_when_main_already_has_the_bad_value(monkeypatch):
    _world(monkeypatch, _deploy(_tol(operator="equal"), image="img:2"),
           _deploy(_tol(operator="equal"), image="img:1"))
    step = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert step[1] is None and step[2] is None
    assert "img:2" in step[0]


def test_run_one_diff_passes_when_the_pr_fixes_it(monkeypatch):
    _world(monkeypatch, _deploy(_tol(operator="Equal")),
           _deploy(_tol(operator="equal")))
    step = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert step[1] is None and "operator: Equal" in step[0]


# ── (c) the retry wrapper, the comment and the status ────────────────────

def _diff(monkeypatch, pr_doc, main_doc):
    _world(monkeypatch, pr_doc, main_doc)
    real, runs = m._run_one_diff, []

    def counting(*a, **k):
        runs.append(1)
        return real(*a, **k)
    monkeypatch.setattr(m, "_run_one_diff", counting)
    return m.argocd_diff(APP, PR_SHA, MAIN_SHA), runs


def test_argocd_diff_is_permanent_after_one_attempt(monkeypatch):
    r, runs = _diff(monkeypatch, _deploy(_tol(operator="equal")), _deploy(_tol()))
    assert len(runs) == 1, "a permanent reason is never retried"
    assert r.outcome == m.OUT_INDETERMINATE
    assert r.reason == m.REASON_SCHEMA_INVALID
    assert OP_LINE in r.error


def test_comment_and_status_name_the_deployment(monkeypatch):
    r, _ = _diff(monkeypatch, _deploy(_tol(operator="equal")), _deploy(_tol()))
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA)
    assert m._extract_status_token(body) == "permanent"
    panel = body.split("RENDER BLOCKED", 1)[1]
    assert "SCHEMA VALIDATION FAILED" in panel
    assert "> " + OP_LINE[2:] in panel
    desc = m._permanent_failure_status_description({APP: r})
    assert desc.startswith(OP_LINE[2:])
    assert "pv-statestreet-c" in desc
    assert desc.endswith("fix and push")


def test_the_panel_says_the_api_and_not_the_schema_rejects_it(monkeypatch):
    """The label says SCHEMA VALIDATION FAILED. The hint says that helm and
    values.schema.json do not check the value, so nobody looks there, and
    that a bad value from the chart needs a chart fix."""
    r, _ = _diff(monkeypatch, _deploy(_tol(operator="equal")), _deploy(_tol()))
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA)
    panel = body.split("RENDER BLOCKED", 1)[1]
    assert schema_errors._TOLERATION_HINT in panel
    assert panel.index(OP_LINE[2:]) < panel.index(
        schema_errors._TOLERATION_HINT)
    assert "`values.schema.json` do not check" in schema_errors._TOLERATION_HINT
    assert "the fix goes in the chart" in schema_errors._TOLERATION_HINT


def test_the_hint_is_only_for_a_toleration():
    null = "- at '/a/b': got null, want object"
    assert schema_errors._TOLERATION_HINT not in m._schema_fix_hints(null)
    both = m._schema_fix_hints(null + "\n" + OP_LINE)
    assert both[0] == schema_errors._TOLERATION_HINT and len(both) == 3
    assert m._schema_fix_hints(OP_LINE) == [schema_errors._TOLERATION_HINT]


def test_a_valid_toleration_change_stays_clean(monkeypatch):
    r, runs = _diff(monkeypatch, _deploy(_tol(operator="Exists")),
                    _deploy(_tol(operator="Equal")))
    assert len(runs) == 1 and r.outcome == m.OUT_DIFF
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA)
    assert m._extract_status_token(body) == "clean"
    assert "RENDER BLOCKED" not in body
