"""Cheap customerName cap replaces the derived GSA name resolution (COPS-2562),
prep-phase fetch/parse reuse, and _redact_sensitive idempotency (COPS-2561).

COPS-2552 shipped a CORRECT check that was too expensive: _pr_gsa_name_checked
resolved prefix/customerName/suffix/esSuffix through each app's ENTIRE
valueFiles chain at BOTH shas, yaml-parsing every file, for every affected
app. On acme-config-prod PR 3831 (a plain version bump, 212 apps, 14 changed
files) the prep phase was ~65s of a 121.5s iteration and the single largest
consumer of Bitbucket API calls -- on a token shared with the Azure DevOps
pipelines (COPS-2543).

The expensive resolution existed only to compute two values that are
constants in practice. Verified across all three config repos on 2026-07-30:

    appspace.prefix                       13 decls, {pv, cl},    always 2 chars
    appspace.suffix                      307 decls, {a, b, c},   always 1 char
    appspace.externalSecretsTool.suffix    0 decls, chart default "es", 2 chars

so len(GSA id) == len(customerName) + 8, and GCP's 30-char cap means
customerName <= 22. The cap is set at 20 to leave two characters of margin
for a future longer prefix/suffix/esSuffix or another derived resource,
without having to re-derive names per resource type again. 322 environments
exist today, the longest customerName is 19, so nothing is grandfathered.
"""
import sys, os, re
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m
import logsink


@pytest.fixture(autouse=True)
def _clear_caches():
    for d in (m._vf_cache, m._vf_inflight):
        d.clear()
    if hasattr(m, "_yaml_cache"):
        m._yaml_cache.clear()
    yield


# ── the cap itself ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("westinghousenuclear", "ok"),       # 19, longest real name in prod today
    ("x" * 20, "ok"),                    # exactly the cap
    ("x" * 21, "invalid"),               # one over
    ("universalhollywood--aec1", "invalid"),  # 24, the original incident
])
def test_customer_name_cap(name, expected):
    status, _ = m._check_customer_name(name)
    assert status == expected


def test_violation_message_names_cap_and_actual_length():
    status, detail = m._check_customer_name("universalhollywood--aec1")
    assert status == "invalid"
    assert "20" in detail          # the cap
    assert "24" in detail          # the actual length
    assert "customerName" in detail


def test_digit_leading_name_is_allowed():
    """pv-3ds-c is a real live prod environment. `3ds` fails the strict GCP
    id regex on its own, but the full id `pv-3ds-c-es` is valid because the
    prefix supplies the required leading letter. Validating customerName with
    the strict regex would wrongly block it."""
    assert m._check_customer_name("3ds")[0] == "ok"


@pytest.mark.parametrize("bad", ["-leading", "trailing-", "Upper", "has_underscore", "has.dot"])
def test_relaxed_charset_rejects_real_gcp_violations(bad):
    assert m._check_customer_name(bad)[0] == "invalid"


def test_empty_or_missing_is_unresolved_not_invalid():
    """Absent is not the same as rejected -- the FIX A (v2.4.9) distinction."""
    assert m._check_customer_name(None)[0] == "unresolved"
    assert m._check_customer_name("")[0] == "unresolved"


# ── reading it: changed files only, never a full chain ──────────────────────

CUST = "gcp/aec/private-cloud/na2-a/pv-acme-a/customer.yaml"
GOOD = "---\nappspace:\n  customerName: acme\n  version: 1.0.0\n"
LONG = "---\nappspace:\n  customerName: universalhollywood--aec1\n  version: 1.0.0\n"


def test_flags_a_changed_file_that_introduces_a_too_long_name(monkeypatch):
    def fake(path, sha, repo=None):
        if path != CUST:
            raise AssertionError(f"must only read CHANGED files, not {path}")
        return (LONG, m.BB_OK) if sha == "prsha" else (GOOD, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    bad = m._changed_files_with_bad_names([CUST], "prsha", "mainsha")
    assert CUST in bad
    assert "24" in bad[CUST]


def test_unchanged_name_is_not_flagged(monkeypatch):
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (LONG, m.BB_OK))
    # Already too long on BOTH sides: the PR does not introduce it, so it is
    # deliberately out of scope (same decision as COPS-2552).
    assert m._changed_files_with_bad_names([CUST], "prsha", "mainsha") == {}


def test_shortening_a_name_is_not_flagged(monkeypatch):
    def fake(path, sha, repo=None):
        return (GOOD, m.BB_OK) if sha == "prsha" else (LONG, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    assert m._changed_files_with_bad_names([CUST], "prsha", "mainsha") == {}


def test_new_file_with_a_bad_name_is_flagged(monkeypatch):
    """An added file (404 at base) must still be validated."""
    def fake(path, sha, repo=None):
        return (LONG, m.BB_OK) if sha == "prsha" else (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    assert CUST in m._changed_files_with_bad_names([CUST], "prsha", "mainsha")


def test_non_identity_files_are_skipped_without_fetching(monkeypatch):
    def fake(path, sha, repo=None):
        raise AssertionError(f"must not fetch a non-identity file: {path}")
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    assert m._changed_files_with_bad_names(
        ["gcp/prod/some/notes.txt", "gcp/prod/some/cicd-versions.yaml"],
        "prsha", "mainsha") == {}


def test_a_pure_version_bump_reads_the_file_once_per_sha(monkeypatch):
    """The whole point of COPS-2562: no value-chain walk. A changed
    customer.yaml is read at most once per sha, and no ancestor is touched."""
    calls = []
    def fake(path, sha, repo=None):
        calls.append((path, sha))
        return (GOOD, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    m._changed_files_with_bad_names([CUST], "prsha", "mainsha")
    assert all(p == CUST for p, _ in calls), f"walked ancestors: {calls}"
    assert len(calls) <= 2, calls


def test_the_expensive_gsa_resolution_is_gone():
    """Regression guard for the point of this ticket: the per-app,
    two-sha, full-chain resolution must not come back."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert "_pr_gsa_name_checked" not in src
    assert "_effective_derived_names" not in src


# ── assumption guard: the cap encodes a config-repo invariant ───────────────

def test_invariant_is_documented_and_warns_when_broken(monkeypatch):
    """The cap is only correct while prefix<=2 and suffix<=1. If a future
    convention changes that, it must surface loudly instead of silently
    making the cap wrong."""
    warned = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: warned.append(msg))
    m._warn_if_name_invariant_broken({"appspace.prefix": "cloud",
                                       "appspace.suffix": "prod"})
    assert warned, "a longer prefix/suffix must produce a warning"
    assert "prefix" in " ".join(warned).lower()


def test_invariant_silent_for_the_normal_case(monkeypatch):
    warned = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: warned.append(msg))
    m._warn_if_name_invariant_broken({"appspace.prefix": "pv",
                                       "appspace.suffix": "a"})
    assert warned == []


# ── point 3: parsed-YAML cache ──────────────────────────────────────────────

def test_parsed_yaml_is_cached_per_sha_and_path(monkeypatch):
    """gcp/config.yaml is 1543 lines and was re-parsed once per app (212x on
    a mass bump). Parsing is cached alongside the text."""
    parses = {"n": 0}
    real_load = m._yaml_safe_load
    def counting_load(s):
        parses["n"] += 1
        return real_load(s)
    monkeypatch.setattr(m, "_yaml_safe_load", counting_load)
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (GOOD, m.BB_OK))
    a = m._flat_yaml_cached(CUST, "prsha")
    b = m._flat_yaml_cached(CUST, "prsha")
    assert a == b
    assert a.get("appspace.customerName") == "acme"
    assert parses["n"] == 1, "second call must hit the parsed cache"


def test_parsed_cache_is_bounded():
    """Same rule as _vf_cache: a pod runs for weeks."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    assert "_bound_yaml_cache" in src or "_yaml_cache" in src
    m._yaml_cache.clear()
    for i in range(m.VF_CACHE_MAX + 50):
        m._yaml_cache[("sha", f"p{i}")] = {}
    m._bound_yaml_cache()
    assert len(m._yaml_cache) <= m.VF_CACHE_MAX
    m._yaml_cache.clear()


def test_unparseable_yaml_is_cached_as_empty_not_retried(monkeypatch):
    parses = {"n": 0}
    def counting_load(s):
        parses["n"] += 1
        raise m.yaml.YAMLError("boom")
    monkeypatch.setattr(m, "_yaml_safe_load", counting_load)
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: ("bad: [", m.BB_OK))
    assert m._flat_yaml_cached(CUST, "prsha") == {}
    assert m._flat_yaml_cached(CUST, "prsha") == {}
    assert parses["n"] == 1


# ── point 2: no untouched file is read at two shas in prep ──────────────────

def test_prep_never_reads_an_untouched_file_at_two_shas():
    """Point 2 turned out to be fully resolved by point 1. The old per-app
    GSA walk was the only prep path that re-read UNTOUCHED ancestor files at
    both shas; every remaining base-sha read is on a file that is changed or
    deleted, where both sides are semantically required. This guard keeps a
    future change from quietly reintroducing a two-sha walk over the whole
    chain."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    prep = src[src.index("def _changed_files_with_bad_names"):
               src.index("def _bb_fetch_cached")]
    # Strip docstrings/comments: the word "ancestor" legitimately appears in
    # the prose explaining that this function does NOT walk one.
    code = "\n".join(l for l in prep.splitlines()
                     if not l.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    # The name check must never build or consume a value chain.
    assert "_new_env_value_chain" not in code
    assert "_effective_chart_version" not in code
    # Every read it makes is of `path`, i.e. a file from the changed list.
    reads = re.findall(r"_flat_yaml_cached\(([^,]+),", code)
    assert reads and all(r.strip() == "path" for r in reads), reads


# ── point 4: one prep pass, not two serial pools ────────────────────────────

def test_prep_runs_as_a_single_pass():
    """chart-revision and the name check used to be two sequential
    ThreadPoolExecutor blocks over the same apps. One pass now answers both."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                            "diff_preview.py")).read()
    prep = src.split("def process_pr", 1)[-1]
    # The name check must no longer have its own executor over `affected`.
    assert "gsa_futs" not in prep, "a second prep pool over affected apps remains"


# ── COPS-2561: _redact_sensitive idempotency ────────────────────────────────
#
# The reported failure was real: _redact_sensitive('\r\r') -> '\n' -> '', so
# the hypothesis property test_redact_is_idempotent failed. The root cause is
# not \r-specific ('\n\n' fails identically): splitlines() + "\n".join()
# drops the trailing terminator, so each pass eats one line ending, and for
# input that is ONLY terminators the string shrinks every call.
#
# The first fix attempt preserved the trailing terminator. The existing
# property test test_redact_never_raises_and_preserves_line_structure caught
# it, and its own comment says the drop is deliberate: "trailing terminator
# dropped -- intended, harmless for the AI prompt", with the contract
# len(out.split("\n")) == max(len(text.splitlines()), 1). The two properties
# cannot both hold for pure-terminator input.
#
# The line-structure contract wins, because it is the one production depends
# on ("redaction must never merge, drop or invent lines, or the diff context
# stops matching the real diff") and idempotency is not: _redact_sensitive is
# never applied twice to the same text anywhere (one call site composes it
# with a DIFFERENT pass, the other follows it with .rstrip()). So the
# function is unchanged and the over-broad property is scoped instead --
# the second option COPS-2561 itself offered.

@pytest.mark.parametrize("text", [
    "a\n", "a\r\nb\n", "+ password: hunter2\n", "", "no trailing terminator",
])
def test_redact_is_idempotent_for_real_content(text):
    """Idempotent for any input containing at least one non-terminator
    character, which is every input production ever passes it."""
    once = m._redact_sensitive(text)
    assert m._redact_sensitive(once) == once


@pytest.mark.parametrize("text", ["\r\r", "\n\n", "\r\n\r\n"])
def test_terminator_only_input_shrinks_by_design(text):
    """Documents the accepted edge: for input that is nothing but line
    terminators, each pass drops one, by the same deliberate rule that drops
    a trailing newline from real text. Asserted explicitly so the behaviour
    is a decision on record rather than a latent surprise."""
    once = m._redact_sensitive(text)
    assert len(once) < len(text)
    assert once.strip() == ""


def test_line_structure_contract_still_holds():
    """The contract the production consumers actually rely on."""
    for text in ("a\nb\nc", "+ password: x\n- token: y", "single"):
        out = m._redact_sensitive(text)
        assert len(out.split("\n")) == max(len(text.splitlines()), 1)


def test_redact_still_redacts():
    out = m._redact_sensitive("+ password: hunter2")
    assert "[REDACTED]" in out and "hunter2" not in out


# ── ported from the COPS-2552 suite: behaviour that outlives the rewrite ────
#
# COPS-2562 deletes _effective_derived_names / _check_gsa_name /
# _pr_gsa_name_checked, so the unit tests for those went with them. These
# four assert behaviour the operator actually depends on, which the cheaper
# implementation must still honour.

def test_reason_is_permanent_not_retryable():
    """A name rejection cannot resolve on retry, only on a new commit."""
    assert m.REASON_NAME_TOO_LONG in m.PERMANENT_REASONS
    assert m.REASON_NAME_TOO_LONG not in m.RETRYABLE_REASONS


def test_new_env_with_a_too_long_name_is_still_blocked(monkeypatch):
    """End-to-end on the new-environment path: the original incident shape
    must still be caught, now via the cheap leaf-file read."""
    env_dir = "gcp/aec/private-cloud/na2-a/pv-universalhollywood--aec1-a"
    cust = f"{env_dir}/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None:
                        (LONG, m.BB_OK) if path == cust else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(
                            AssertionError("must not render a name that cannot deploy")))
    env_info = {"name": "pv-universalhollywood--aec1-a", "config_file": cust,
                "env_dir": env_dir, "all_yaml_files": [cust]}
    lines, structural, _ = m._evaluate_new_envs([env_info], "prsha")
    assert structural == ["pv-universalhollywood--aec1-a"]
    joined = "\n".join(lines)
    assert "24" in joined and "20" in joined


def test_new_env_with_a_valid_name_is_not_blocked(monkeypatch):
    env_dir = "gcp/aec/private-cloud/na2-a/pv-acme-a"
    cust = f"{env_dir}/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None:
                        (GOOD, m.BB_OK) if path == cust else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (None,
                            "helm template failed: Missing required value: x", 0, None))
    env_info = {"name": "pv-acme-a", "config_file": cust,
                "env_dir": env_dir, "all_yaml_files": [cust]}
    _lines, structural, _ = m._evaluate_new_envs([env_info], "prsha")
    assert structural == []


def test_blocked_headline_names_the_length_problem_not_the_cohort(monkeypatch):
    """Regression guard from the COPS-2552 follow-up: three findings share
    the blocking path, so the headline must come from the finding."""
    env_dir = "gcp/aec/private-cloud/na2-a/pv-universalhollywood--aec1-a"
    cust = f"{env_dir}/customer.yaml"
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None:
                        (LONG, m.BB_OK) if path == cust else (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(AssertionError("no render")))
    env_info = {"name": "pv-universalhollywood--aec1-a", "config_file": cust,
                "env_dir": env_dir, "all_yaml_files": [cust]}
    lines, _s, _t = m._evaluate_new_envs([env_info], "prsha")
    joined = "\n".join(lines)
    assert "cohort" not in joined.lower()
    assert "too long for GCP" in joined


def test_blocked_headline_still_names_the_cohort_when_that_is_the_reason(monkeypatch):
    env_dir = "gcp/prod/private-cloud/gb1-b/hardcoded/migrationtest/pv-copstest-a"
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (None, m.BB_NOT_FOUND))
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda info, sha: (_ for _ in ()).throw(AssertionError("no render")))
    env_info = {"name": "pv-copstest-a", "config_file": f"{env_dir}/customer.yaml",
                "env_dir": env_dir, "all_yaml_files": [f"{env_dir}/customer.yaml"]}
    lines, structural, _ = m._evaluate_new_envs([env_info], "prsha")
    assert structural == ["pv-copstest-a"]
    assert "cohort" in "\n".join(lines).lower()


# ── hardening from the pre-PR review pass (2026-07-30) ──────────────────────
#
# Five gaps between the first implementation and contracts the rest of the
# module already promises. Each test pins one so it cannot quietly return.

def test_transient_fetch_error_is_never_cached_as_a_fact(monkeypatch):
    """BB_ERROR is documented on the constant itself as NOT cacheable. A
    rate-limited fetch must not pin {} into _yaml_cache, or a single 429
    would silently disable the name check for that (sha, path) for the
    lifetime of the pod. The next call must retry and see real content."""
    calls = []
    def flaky(path, sha, repo=None):
        calls.append(1)
        return (None, m.BB_ERROR) if len(calls) == 1 else (LONG, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", flaky)
    assert m._flat_yaml_cached(CUST, "prsha") == {}
    flat = m._flat_yaml_cached(CUST, "prsha")
    assert flat.get("appspace.customerName") == "universalhollywood--aec1"
    assert len(calls) == 2


def test_a_404_at_a_sha_is_a_fact_and_caches(monkeypatch):
    """The counterpart: content at an immutable sha, including its absence,
    IS a stable fact. Only BB_ERROR must stay uncached."""
    calls = []
    def gone(path, sha, repo=None):
        calls.append(1)
        return (None, m.BB_NOT_FOUND)
    monkeypatch.setattr(m, "_bb_fetch_status", gone)
    assert m._flat_yaml_cached(CUST, "prsha") == {}
    assert m._flat_yaml_cached(CUST, "prsha") == {}
    assert len(calls) == 1, "a 404 at an immutable sha must be fetched once"


def test_yaml_cache_is_bounded_through_the_production_path(monkeypatch):
    """Defining _bound_yaml_cache is not enough -- it must run on the code
    path that inserts. The first implementation defined the bound and never
    called it: an unbounded module-level cache, exactly the leak class
    COPS-2546 closed."""
    monkeypatch.setattr(m, "VF_CACHE_MAX", 16)
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (GOOD, m.BB_OK))
    for i in range(50):
        m._flat_yaml_cached(f"gcp/x/e{i}/customer.yaml", "prsha")
    assert len(m._yaml_cache) <= 16


def test_lookalike_basenames_are_not_identity_files(monkeypatch):
    """Exact basename membership, not endswith: 'mycustomer.yaml' and
    'app-config.yaml' merely END WITH the identity basenames. They must be
    skipped without a single fetch, same as any other non-identity file."""
    def fake(path, sha, repo=None):
        raise AssertionError(f"must not fetch a lookalike file: {path}")
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    assert m._changed_files_with_bad_names(
        ["gcp/prod/x/mycustomer.yaml", "gcp/prod/x/app-config.yaml"],
        "prsha", "mainsha") == {}


def test_invariant_warning_fires_for_a_tier_file_without_a_name(monkeypatch):
    """Tier config.yaml files declare prefix/suffix WITHOUT a customerName,
    and they are exactly where a longer prefix would first appear. The
    warning must fire before the no-customerName skip, not after it."""
    tier = "gcp/aec/private-cloud/na2-a/config.yaml"
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None:
                        ("---\nappspace:\n  prefix: cloud\n", m.BB_OK))
    warned = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: warned.append(msg))
    assert m._changed_files_with_bad_names([tier], "prsha", "mainsha") == {}
    assert any("prefix" in w for w in warned), (
        "a 3+ char prefix in a changed tier file must warn even when the "
        "file declares no customerName")


def test_one_broken_file_does_not_kill_the_whole_check(monkeypatch):
    """The old per-app pool swallowed exceptions per future. The single-pass
    replacement must keep that property: a file whose fetch raises fails
    open (skipped, loud warning) while every other file is still checked."""
    broken = "gcp/x/pv-broken-a/customer.yaml"
    def fake(path, sha, repo=None):
        if path == broken:
            raise RuntimeError("boom")
        return (LONG, m.BB_OK) if sha == "prsha" else (GOOD, m.BB_OK)
    monkeypatch.setattr(m, "_bb_fetch_status", fake)
    warned = []
    monkeypatch.setattr(logsink, "log", lambda msg, *a, **k: warned.append(msg))
    bad = m._changed_files_with_bad_names([broken, CUST], "prsha", "mainsha")
    assert CUST in bad and broken not in bad
    assert any("fail-open" in w for w in warned)


def test_cache_key_is_normalized_like_the_text_cache(monkeypatch):
    """"$config/x.yaml" and "x.yaml" are the same file. The parse cache must
    key them identically, as _bb_fetch_cached already does for the text."""
    monkeypatch.setattr(m, "_bb_fetch_status",
                        lambda path, sha, repo=None: (GOOD, m.BB_OK))
    a = m._flat_yaml_cached("$config/" + CUST, "prsha")
    b = m._flat_yaml_cached(CUST, "prsha")
    assert a == b
    assert len(m._yaml_cache) == 1, "one file, one parse, one entry"
