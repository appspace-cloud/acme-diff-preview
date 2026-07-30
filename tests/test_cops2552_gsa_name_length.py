"""Block PRs whose derived GCP service account name violates GCP's own limits.

Live incident (COPS-2552, reported by Derek 2026-07-29): pv-universalhollywood--aec1-a
never came up. ArgoCD reported Synced the whole time, health Degraded, 198/199
pods stuck in CreateContainerConfigError ("secret mongodb-password not
found"). The Secret Manager secret existed, which is what made it look like
an ArgoCD bug. The actual failure:

    IAMServiceAccount pv-universalhollywood--aec1-a-es: UpdateFailed
    Error 400: The account ID "pv-universalhollywood--aec1-a-es" does not
    have length between 6 and 30., badRequest

That ID is 32 characters; GCP allows 6-30. Helm renders the manifest fine
(valid YAML, valid k8s object name -- k8s allows 253 chars) and ArgoCD
applies it successfully, so nothing render- or sync-based can ever catch
this. It is a GCP IAM API rejection inside the Config Connector reconcile
loop, so it needs an explicit assertion on the resolved config values,
before merge.

GSA ID = "{prefix}-{customerName}-{suffix}-{esSuffix}"
(appspace.fullcustomername = prefix-customerName-suffix, chart's own
_helpers.tpl; esSuffix defaults to "es"). For the standard prefix=pv,
suffix=a: GSA ID length = len(customerName) + 8, so customerName max is 22.
universalhollywood--aec1 is 24 chars -> 32 total, over by 2.
"""
import sys, os
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


ROOT   = "$config/gcp/config.yaml"
TIER   = "$config/gcp/aec/private-cloud/config.yaml"
CUST   = "$config/gcp/aec/private-cloud/na2-a/pv-universalhollywood--aec1-a/customer.yaml"

ROOT_CONTENT = "---\nappspace:\n  cloudShortName: gcp\n"
TIER_CONTENT = "---\nappspace:\n  prefix: pv\n  suffix: a\n"
LIVE_CUST_CONTENT = (
    "---\nappspace:\n  version: 2603.0.12-rev1\n"
    "  customerName: universalhollywood--aec1\n  instance: pv-universalhollywood--aec1-a\n"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    for d in (m._vf_cache, m._vf_inflight):
        d.clear()
    yield


# ── _effective_derived_names: multi-file resolution ─────────────────────────

def test_resolves_prefix_suffix_from_tier_and_customer_name_from_leaf():
    ordered = [ROOT, TIER, CUST]
    vals = {ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: LIVE_CUST_CONTENT}
    names = m._effective_derived_names(ordered, vals)
    assert names == {"prefix": "pv", "customerName": "universalhollywood--aec1",
                      "suffix": "a", "esSuffix": "es"}


def test_missing_ancestor_file_is_skipped_not_fatal():
    ordered = [ROOT, TIER, CUST]
    vals = {CUST: LIVE_CUST_CONTENT}  # TIER 404'd
    names = m._effective_derived_names(ordered, vals)
    assert names["customerName"] == "universalhollywood--aec1"
    assert names["prefix"] is None  # genuinely unresolved, not guessed


def test_later_file_overrides_earlier_one_per_key():
    """Real helm -f semantics: last file wins PER KEY, not wholesale."""
    ordered = [ROOT, TIER, CUST]
    override_cust = LIVE_CUST_CONTENT.replace("customerName: universalhollywood--aec1",
                                                "customerName: universalhollywood--aec1\n  suffix: b")
    vals = {ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: override_cust}
    names = m._effective_derived_names(ordered, vals)
    assert names["suffix"] == "b"       # leaf override wins
    assert names["prefix"] == "pv"      # tier value still resolves


def test_es_suffix_defaults_to_es_when_never_declared():
    ordered = [CUST]
    vals = {CUST: LIVE_CUST_CONTENT}
    names = m._effective_derived_names(ordered, vals)
    assert names["esSuffix"] == "es"


def test_unparseable_file_in_the_chain_is_skipped_not_fatal():
    ordered = [ROOT, CUST]
    vals = {ROOT: "not: valid: yaml: [", CUST: LIVE_CUST_CONTENT}
    names = m._effective_derived_names(ordered, vals)
    assert names["customerName"] == "universalhollywood--aec1"


# ── _check_gsa_name: the boundary, the live case, and the char pattern ──────

def test_live_incident_case_is_rejected_with_the_right_arithmetic():
    names = {"prefix": "pv", "customerName": "universalhollywood--aec1",
             "suffix": "a", "esSuffix": "es"}
    status, detail = m._check_gsa_name(names)
    assert status == "invalid"
    assert "pv-universalhollywood--aec1-a-es" in detail
    assert "32" in detail
    assert "22" in detail  # max customerName length for this prefix/suffix
    assert "6" in detail and "30" in detail


def test_control_case_pv_pfizer_is_accepted():
    """pv-pfizer--aec1-a-es, 20 chars: the live control case that worked."""
    names = {"prefix": "pv", "customerName": "pfizer--aec1",
             "suffix": "a", "esSuffix": "es"}
    status, detail = m._check_gsa_name(names)
    assert status == "ok"
    assert detail is None


@pytest.mark.parametrize("customer_len,expected_status", [
    (21, "ok"),       # 2+1+21+1+1+1+2 = 29
    (22, "ok"),       # exactly 30, the boundary
    (23, "invalid"),  # 31, one over
])
def test_length_boundary_29_30_31(customer_len, expected_status):
    names = {"prefix": "pv", "customerName": "x" * customer_len,
             "suffix": "a", "esSuffix": "es"}
    status, _ = m._check_gsa_name(names)
    assert status == expected_status, (
        f"customerName len {customer_len} -> gsa len "
        f"{len('pv-' + 'x'*customer_len + '-a-es')}")


def test_six_char_lower_bound():
    # prefix=p, customerName=c, suffix=s, esSuffix=es -> "p-c-s-es" = 8 chars, ok
    assert m._check_gsa_name(
        {"prefix": "p", "customerName": "c", "suffix": "s", "esSuffix": "es"}
    )[0] == "ok"
    # A 5-char total cannot actually be built from 4 non-empty hyphen-joined
    # parts (minimum is "p-c-s-e" = 7), so assert the arithmetic directly
    # instead of contriving an unreachable 5-char case.
    short = {"prefix": "p", "customerName": "c", "suffix": "s", "esSuffix": "e"}
    gsa_id = "-".join([short["prefix"], short["customerName"],
                        short["suffix"], short["esSuffix"]])
    assert len(gsa_id) == 7  # above the 6-char minimum; sanity check only


def test_non_default_prefix_and_suffix_change_the_max():
    """A longer prefix/suffix combination must lower the allowed customerName
    length accordingly -- the limit is on the WHOLE derived id, not customerName
    alone."""
    names = {"prefix": "cloud", "customerName": "x" * 20, "suffix": "prod", "esSuffix": "es"}
    # "cloud-" (6) + 20 + "-prod-es" (8) = 34
    status, detail = m._check_gsa_name(names)
    assert status == "invalid"
    assert "cloud" in detail and "prod" in detail


def test_uppercase_in_customer_name_is_rejected():
    """GCP account IDs must be all lowercase; a typo'd uppercase customerName
    is a real 400 too, independent of length."""
    names = {"prefix": "pv", "customerName": "Acme", "suffix": "a", "esSuffix": "es"}
    status, detail = m._check_gsa_name(names)
    assert status == "invalid"


def test_trailing_hyphen_is_rejected():
    """An empty final component (e.g. esSuffix explicitly blanked) leaves a
    trailing hyphen, which GCP also rejects, independent of length."""
    names = {"prefix": "pv", "customerName": "acme", "suffix": "a", "esSuffix": ""}
    status, detail = m._check_gsa_name(names)
    assert status == "invalid"


def test_unresolved_names_do_not_block():
    """Missing prefix/customerName/suffix anywhere in the chain means this
    check has nothing to validate -- must not be confused with 'invalid'."""
    status, detail = m._check_gsa_name(
        {"prefix": None, "customerName": "x", "suffix": "a", "esSuffix": "es"})
    assert status == "unresolved"
    assert detail is None


# ── reason classification: permanent, never retried ─────────────────────────

def test_reason_is_permanent_not_retryable():
    assert m.REASON_NAME_TOO_LONG in m.PERMANENT_REASONS
    assert m.REASON_NAME_TOO_LONG not in m.RETRYABLE_REASONS


# ── hook point 1: new-environment path ──────────────────────────────────────

def test_new_env_candidate_with_too_long_a_name_is_blocked(monkeypatch):
    # _bb_fetch_status always receives the CLEAN path (the "$config/" alias
    # is stripped one layer up, inside _fetch_value_files) -- the mock must
    # key on that, not on the prefixed form used elsewhere in this file.
    files = {p.replace("$config/", ""): c for p, c in
             {ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: LIVE_CUST_CONTENT}.items()}
    def fake_fetch(path, sha, repo=None):
        return (files[path], m.BB_OK) if path in files else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(
                            AssertionError("must not render a name that will never deploy")))
    env_info = {"name": "pv-universalhollywood--aec1-a",
                "config_file": CUST.replace("$config/", ""),
                "env_dir": CUST.replace("$config/", "").rsplit("/", 1)[0],
                "all_yaml_files": [CUST.replace("$config/", "")]}
    lines, structural, total = m._evaluate_new_envs([env_info], "prsha")
    assert structural == ["pv-universalhollywood--aec1-a"]
    joined = "\n".join(lines)
    assert "pv-universalhollywood--aec1-a-es" in joined
    assert "32" in joined and "22" in joined


def test_new_env_candidate_with_a_short_name_is_not_blocked(monkeypatch):
    short_cust_content = LIVE_CUST_CONTENT.replace(
        "customerName: universalhollywood--aec1", "customerName: pfizer--aec1")
    files = {p.replace("$config/", ""): c for p, c in
             {ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: short_cust_content}.items()}
    def fake_fetch(path, sha, repo=None):
        return (files[path], m.BB_OK) if path in files else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (None,
                            "helm template failed: Missing required value: x", 0, None))
    env_info = {"name": "pv-pfizer--aec1-a",
                "config_file": CUST.replace("$config/", ""),
                "env_dir": CUST.replace("$config/", "").rsplit("/", 1)[0],
                "all_yaml_files": [CUST.replace("$config/", "")]}
    lines, structural, total = m._evaluate_new_envs([env_info], "prsha")
    assert structural == []


# ── hook point 2: existing-environment rename path ──────────────────────────

def test_existing_env_rename_that_breaks_the_name_is_blocked(monkeypatch):
    app = "pv-universalhollywood--aec1-a-ms"
    files = [ROOT, TIER, CUST]
    monkeypatch.setattr(m, "_app_value_files_map", {app: files})
    def clean(d):
        return {p.replace("$config/", ""): c for p, c in d.items()}
    main_files = clean({ROOT: ROOT_CONTENT, TIER: TIER_CONTENT,
                  CUST: LIVE_CUST_CONTENT.replace("universalhollywood--aec1", "univhol")})
    pr_files = clean({ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: LIVE_CUST_CONTENT})
    def fake_fetch(path, sha, repo=None):
        src = pr_files if sha == "prsha" else main_files
        return (src[path], m.BB_OK) if path in src else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    blocked, detail = m._pr_gsa_name_checked(app, "prsha", "mainsha")
    assert blocked is True
    assert "32" in detail


def test_existing_env_untouched_name_is_never_blocked_even_if_already_invalid(monkeypatch):
    """Suggested scope from the ticket: validating unconditionally would also
    block unrelated PRs that merely touch an already-broken env. Out of
    scope by design."""
    app = "pv-universalhollywood--aec1-a-ms"
    files = [ROOT, TIER, CUST]
    monkeypatch.setattr(m, "_app_value_files_map", {app: files})
    same_files = {p.replace("$config/", ""): c for p, c in
                  {ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: LIVE_CUST_CONTENT}.items()}
    def fake_fetch(path, sha, repo=None):
        return (same_files[path], m.BB_OK) if path in same_files else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    blocked, detail = m._pr_gsa_name_checked(app, "prsha", "mainsha")
    assert blocked is False


def test_existing_env_rename_to_a_valid_name_is_not_blocked(monkeypatch):
    app = "pv-universalhollywood--aec1-a-ms"
    files = [ROOT, TIER, CUST]
    monkeypatch.setattr(m, "_app_value_files_map", {app: files})
    def clean(d):
        return {p.replace("$config/", ""): c for p, c in d.items()}
    main_files = clean({ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: LIVE_CUST_CONTENT})
    pr_files = clean({ROOT: ROOT_CONTENT, TIER: TIER_CONTENT,
                CUST: LIVE_CUST_CONTENT.replace("universalhollywood--aec1", "univhol")})
    def fake_fetch(path, sha, repo=None):
        src = pr_files if sha == "prsha" else main_files
        return (src[path], m.BB_OK) if path in src else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fake_fetch)
    blocked, detail = m._pr_gsa_name_checked(app, "prsha", "mainsha")
    assert blocked is False


def test_app_with_no_cached_value_files_is_never_blocked(monkeypatch):
    monkeypatch.setattr(m, "_app_value_files_map", {})
    blocked, detail = m._pr_gsa_name_checked("some-app-ms", "prsha", "mainsha")
    assert blocked is False


# ── wired check: the guard must actually be reachable, not merely defined ───

def test_process_pr_wires_the_gsa_name_guard():
    """The guard must actually be reachable, not merely defined -- exactly
    the failure class COPS-2553 fixed for the cohort-move guard. Called via
    ex.submit(_pr_gsa_name_checked, ...) (a bare reference, no parens),
    the same convention _pr_chart_revision_checked already uses, so check
    for the reference rather than a direct-call parenthesized form."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    body = src.replace("def _pr_gsa_name_checked(", "", 1)
    assert "_pr_gsa_name_checked" in body, "guard is defined but never referenced"
    assert "gsa_invalid_apps" in src.split("def process_pr", 1)[-1], (
        "guard result never reaches the diff/status path")
    assert "REASON_NAME_TOO_LONG" in src.split("def process_pr", 1)[-1], (
        "the blocking reason never reaches the DiffResult for run_diff")


def test_new_env_value_chain_shared_helper_exists_and_is_used_by_render():
    """Regression guard for the refactor: _render_new_env_diff must use the
    same shared ancestor-chain builder the GSA check uses, not a second,
    independently-maintained copy of the same logic."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    render_body_start = src.index("def _render_new_env_diff(")
    render_body = src[render_body_start:render_body_start + 4000]
    assert "_new_env_value_chain(" in render_body


# ── the blocked-block headline must match the ACTUAL reason ─────────────────
#
# Found by the live verification on PR 3830, not by any unit test: the
# GSA-name block reused COPS-2544's blocking path, whose headline is
# hardcoded to "a required cohort config.yaml is missing". That headline is
# simply false for a name-length rejection and directly contradicts the
# correct explanation printed right underneath it -- exactly the kind of
# self-contradicting output an operator has to untangle. Three different
# reasons now share this path (missing cohort, unparseable cohort, name too
# long), so the headline has to come from the finding, not be baked in.

def test_blocked_headline_names_the_length_problem_not_the_cohort(monkeypatch):
    files = {p.replace("$config/", ""): c for p, c in
             {ROOT: ROOT_CONTENT, TIER: TIER_CONTENT, CUST: LIVE_CUST_CONTENT}.items()}
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None:
                        (files[path], m.BB_OK) if path in files else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(AssertionError("no render")))
    env_info = {"name": "pv-universalhollywood--aec1-a",
                "config_file": CUST.replace("$config/", ""),
                "env_dir": CUST.replace("$config/", "").rsplit("/", 1)[0],
                "all_yaml_files": [CUST.replace("$config/", "")]}
    lines, structural, _ = m._evaluate_new_envs([env_info], "prsha")
    joined = "\n".join(lines)
    assert "cohort" not in joined.lower(), (
        "a name-length rejection must not be announced as a missing cohort file")
    assert "too long for GCP" in joined


def test_blocked_headline_still_names_the_cohort_when_that_is_the_reason(monkeypatch):
    """Regression guard: COPS-2544's own case must keep its accurate headline."""
    cohort = "gcp/prod/private-cloud/gb1-b/hardcoded/migrationtest/config.yaml"
    env_dir = "gcp/prod/private-cloud/gb1-b/hardcoded/migrationtest/pv-copstest-a"
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(AssertionError("no render")))
    env_info = {"name": "pv-copstest-a", "config_file": f"{env_dir}/customer.yaml",
                "env_dir": env_dir, "all_yaml_files": [f"{env_dir}/customer.yaml"]}
    lines, structural, _ = m._evaluate_new_envs([env_info], "prsha")
    joined = "\n".join(lines)
    assert structural == ["pv-copstest-a"]
    assert "cohort" in joined.lower() and cohort in joined
