"""Full rendered output for new environments (v2.25.0).

Live gap, acme-config-prod PR #3863 (new env, 370 resources incl. 1 Secret):
the bot posted only a provisioning summary — the complete rendered manifest
was discarded after _summarize_rendered_manifest, and the new-env-only path
never saved a full-diff UI artifact, so there was no record anywhere of what
a brand-new environment would actually ship as. These tests pin the fix:

  * a redacted full-output appendix, returned separately (opt-in) so the
    summary block's existing contract is untouched;
  * the appendix spliced at the END of the comment body (never starving
    existing-app diffs out of the truncation budget);
  * the new-env-only orchestrator path persisting the untruncated body as
    the full-diff artifact BEFORE the final build status, so the status
    icon can deep-link to the complete page.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m


RENDERED = """kind: Deployment
apiVersion: apps/v1
metadata:
  name: api-gateway
spec:
  template:
    spec:
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - podAffinityTerm:
              topologyKey: kubernetes.io/hostname
      nodeSelector:
        key: workload-pool
      containers:
      - name: api-gateway
        env:
        - name: REDIS_HOST
          value: redis.internal
---
kind: Secret
apiVersion: v1
metadata:
  name: platform-secrets
type: Opaque
stringData:
  redisPass: hunter2plaintext
  apiToken: tok-swordfish-99
---
kind: ExternalSecret
apiVersion: external-secrets.io/v1beta1
metadata:
  name: vault-ref
spec:
  data:
  - remoteRef:
      key: platform/vault-path-visible
---
kind: Service
apiVersion: v1
metadata:
  name: account
"""


def _mk_candidate(name="pv-full-a"):
    return [{"name": name, "config_file": "x/customer.yaml", "env_dir": "x",
             "all_yaml_files": ["x/customer.yaml"], "version": "2603.0.1-dev"}]


# ── the redaction helper ────────────────────────────────────────────────────

def test_redact_rendered_manifest_whole_masks_v1_secret_docs():
    out = m._redact_rendered_manifest(RENDERED)
    assert "hunter2plaintext" not in out
    assert "tok-swordfish-99" not in out
    # the Secret document itself must still be visible as a resource:
    # identity (kind + name) survives, only the values are masked
    assert "platform-secrets" in out
    assert "kind: Secret" in out


def test_redact_rendered_manifest_does_not_whole_mask_external_secret():
    out = m._redact_rendered_manifest(RENDERED)
    # ExternalSecret holds references, not values — the vault path a
    # reviewer needs must survive.
    assert "platform/vault-path-visible" in out


def test_redact_rendered_manifest_keeps_scheduling_fields():
    # `key:` / `topologyKey:` are structural scheduling fields, exempt from
    # the key-name redaction — regression guard for the false-positive
    # masking fixed in v2.20.0.
    out = m._redact_rendered_manifest(RENDERED)
    assert "topologyKey: kubernetes.io/hostname" in out
    assert "key: workload-pool" in out


# ── _evaluate_new_envs: contract and appendix ───────────────────────────────

def test_evaluate_new_envs_default_contract_is_still_three_tuple(monkeypatch):
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (RENDERED, None, 4, "2603.0.1-dev"))
    result = m._evaluate_new_envs(_mk_candidate(), "prsha")
    assert len(result) == 3


def test_evaluate_new_envs_with_full_output_returns_appendix(monkeypatch):
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (RENDERED, None, 4, "2603.0.1-dev"))
    lines, structural, total, full_lines = m._evaluate_new_envs(
        _mk_candidate(), "prsha", with_full_output=True)
    summary = "\n".join(lines)
    appendix = "\n".join(full_lines)
    # summary block keeps its shape: no manifest wall in it
    assert "kind: Deployment" not in summary
    # the appendix carries the complete (redacted) manifest in a yaml fence
    assert "Full rendered output" in appendix
    assert "```yaml" in appendix
    assert "kind: Deployment" in appendix
    assert "kind: Service" in appendix
    assert "pv-full-a" in appendix
    assert "hunter2plaintext" not in appendix
    assert structural == [] and total == 4


def test_evaluate_new_envs_appendix_empty_when_render_fails(monkeypatch):
    monkeypatch.setattr(m, "_render_new_env_diff",
        lambda env_info, pr_sha: (None, "helm template failed: Missing required value: x", 0, None))
    lines, structural, total, full_lines = m._evaluate_new_envs(
        _mk_candidate(), "prsha", with_full_output=True)
    assert full_lines == []


# ── format_comment: appendix placement ──────────────────────────────────────

def test_format_comment_places_appendix_after_new_env_and_before_footer():
    body = m.format_comment(
        "c" * 12, {}, new_env_lines=["NEWENV_SENTINEL"],
        appendix_lines=["APPENDIX_SENTINEL"])
    i_env = body.index("NEWENV_SENTINEL")
    i_app = body.index("APPENDIX_SENTINEL")
    i_foot = body.index("**Status:**")
    assert i_env < i_app < i_foot


def test_truncation_keeps_footer_with_giant_appendix():
    giant = ["```yaml"] + [f"kind: ConfigMap  # line {i}" for i in range(20000)] + ["```"]
    body = m.format_comment("c" * 12, {}, new_env_lines=["NEWENV_SENTINEL"],
                            appendix_lines=giant)
    assert len(body.encode()) > m.MAX_COMMENT_BYTES
    cut = m._truncate_comment(body)
    assert len(cut.encode()) <= m.MAX_COMMENT_BYTES
    assert "**Status:**" in cut
    assert m.COMMENT_MARKER in cut
    # the summary at the top survives; the appendix is what gets cut
    assert "NEWENV_SENTINEL" in cut
    assert cut.count("```") % 2 == 0


# ── orchestrator: new-env-only path persists the artifact ──────────────────

_PR_SHA = "aabbccddeeffprsha"
_BASE_SHA = "112233445566mainsha"


def _mk_pr(pr_id=3863):
    return {
        "id": pr_id,
        "title": "synthetic new-env PR",
        "source": {"commit": {"hash": _PR_SHA}, "branch": {"name": "new-env"}},
        "destination": {"branch": {"name": "main"}},
    }


def test_newenv_only_path_saves_artifact_before_final_status(monkeypatch):
    m._seen.clear()
    m._force_recompute.clear()
    events = []
    monkeypatch.setattr(m, "get_pr_changed_files",
                        lambda pr_id, repo=None: (["x/customer.yaml"], {}))
    monkeypatch.setattr(m, "find_existing_comment", lambda pr_id, repo=None: (None, "", ""))
    monkeypatch.setattr(m, "fix_stuck_inprogress", lambda *a, **k: None)
    monkeypatch.setattr(m, "_touch_progress", lambda: None)
    monkeypatch.setattr(m, "_detect_env_decommission_candidates",
                        lambda *a, **k: [])
    monkeypatch.setattr(m, "_detect_new_env_candidates",
                        lambda *a, **k: _mk_candidate())
    monkeypatch.setattr(m, "_flat_yaml_cached",
                        lambda path, sha: {"appspace.customerName": "pv-full"})
    monkeypatch.setattr(m, "_render_new_env_diff",
                        lambda env_info, pr_sha: (RENDERED, None, 4, "2603.0.1-dev"))
    monkeypatch.setattr(m, "post_build_status",
                        lambda pr_sha, state, description, pr_id=None, repo=None:
                        events.append(("status", state)))
    monkeypatch.setattr(m, "_save_diff_ui_artifact",
                        lambda repo, pr_id, pr_sha, body, **kw:
                        events.append(("artifact", body)))
    monkeypatch.setattr(m, "upsert_comment",
                        lambda pr_id, body, existing_id=None, repo=None:
                        events.append(("comment", body)) or 1)
    try:
        m.process_pr(_mk_pr(), {}, base_sha=_BASE_SHA)
    finally:
        m._seen.clear()
        m._force_recompute.clear()

    kinds = [e[0] for e in events]
    assert "artifact" in kinds, f"no artifact saved on the new-env-only path: {kinds}"
    assert "comment" in kinds
    art_body = next(b for k, b in events if k == "artifact")
    com_body = next(b for k, b in events if k == "comment")
    # the artifact holds the complete output and matches what upsert got
    # (upsert does its own truncation afterwards)
    assert "kind: Deployment" in art_body
    assert "hunter2plaintext" not in art_body
    assert art_body == com_body
    # the artifact is saved BEFORE the final (non-INPROGRESS) build status,
    # so the status icon can deep-link to the full page
    final_status_idx = max(i for i, e in enumerate(events) if e[0] == "status")
    artifact_idx = next(i for i, e in enumerate(events) if e[0] == "artifact")
    assert artifact_idx < final_status_idx
