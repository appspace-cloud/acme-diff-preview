"""The AI summary request on Gemini 4: global endpoint, thinking level, no
retired settings, and only the answer text from the reply.

In this project Vertex AI serves the Gemini 3 and 4 models only on the global
endpoint, whose host has no region prefix. Gemini 4 refuses thinkingBudget
with a 400 and ignores temperature, and thinking tokens count against
maxOutputTokens, so a small cap cuts the answer off.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402

BODY = "--- \n+++ \n@@ -1,3 +1,3 @@\n spec:\n-  replicas: 2\n+  replicas: 3\n"
ENV = ("VERTEX_LOCATION", "VERTEX_MODEL", "VERTEX_THINKING_LEVEL", "AI_SUMMARY_ENABLED")


@pytest.fixture(autouse=True)
def _reload_defaults_after_each_test():
    # importlib.reload changes the shared module object, so put the defaults
    # back or later test files see this file's settings.
    yield
    for k in ENV:
        os.environ.pop(k, None)
    importlib.reload(m)


def _reload(monkeypatch, **env):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AI_SUMMARY_ENABLED", "true")
    importlib.reload(m)
    return m


def _results():
    return {"pv-example-a-ms": m.DiffResult(
        "===== /apps/Deployment account =====\n" + BODY,
        [("/apps/Deployment account", BODY)], 1, True, None,
        m.OUT_DIFF, "diff", None, None, None)}


def _capture(monkeypatch, mod, reply=None):
    seen = {}

    def fake_http(method, url, body=None, headers=None, auth=None):
        seen["url"], seen["body"] = url, body
        if reply is None:
            raise RuntimeError("stop after capture")
        return reply

    monkeypatch.setattr(mod, "http", fake_http)
    monkeypatch.setattr(mod, "_gcp_access_token", lambda: "tok")
    out = mod.generate_ai_summary(_results())
    return seen, out


def test_defaults_are_gemini_4_on_the_global_endpoint(monkeypatch):
    mod = _reload(monkeypatch)
    assert (mod.VERTEX_LOCATION, mod.VERTEX_MODEL, mod.VERTEX_THINKING_LEVEL) == (
        "global", "gemini-4-argon", "LOW")


def test_the_global_location_calls_the_host_without_a_region(monkeypatch):
    mod = _reload(monkeypatch)
    seen, _ = _capture(monkeypatch, mod)
    assert seen["url"].startswith("https://aiplatform.googleapis.com/v1/projects/")
    assert seen["url"].endswith(
        "/locations/global/publishers/google/models/gemini-4-argon:generateContent")


def test_a_regional_location_keeps_its_regional_host(monkeypatch):
    mod = _reload(monkeypatch, VERTEX_LOCATION="us-central1")
    seen, _ = _capture(monkeypatch, mod)
    assert seen["url"].startswith("https://us-central1-aiplatform.googleapis.com/v1/")


def test_the_request_sends_a_thinking_level_and_no_retired_settings(monkeypatch):
    mod = _reload(monkeypatch)
    seen, _ = _capture(monkeypatch, mod)
    gen = seen["body"]["generationConfig"]
    assert gen.get("thinkingConfig") == {"thinkingLevel": "LOW"}
    assert "temperature" not in gen
    assert gen["maxOutputTokens"] >= 32768


def test_the_thinking_level_is_read_in_any_case(monkeypatch):
    mod = _reload(monkeypatch, VERTEX_THINKING_LEVEL="medium")
    seen, _ = _capture(monkeypatch, mod)
    assert seen["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}


def test_the_summary_keeps_only_answer_text_not_thoughts(monkeypatch):
    mod = _reload(monkeypatch)
    reply = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "private reasoning", "thought": True},
        {"text": "account: replicas 2 to 3."},
    ]}}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}
    _, out = _capture(monkeypatch, mod, reply)
    assert out and "replicas 2 to 3" in out
    assert "private reasoning" not in out
