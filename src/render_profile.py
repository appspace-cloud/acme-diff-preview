"""How much of the truth a surface shows, and the app block that renders it.

The render profile turns "comment or full page" from an integer budget into
a value: phases C, D and E of COPS-2607 change a profile rather than the
renderer. Its env constants live here with it, and that is the point of the
module rather than an accident of packing.

`resolved()` reads those constants when a render happens, never at import.
A snapshot taken at import would make every one of them decorative -- the
env var ignored, the phase E rollback inert on a running pod, and the
symptom a comment that quietly stops honouring its own configured budget.
That is the bug the readable_budget_collapse golden caught in COPS-2607.

Which is also why the constants could not stay behind when the reader
moved. A patch reaches a name only through the namespace that reads it, so
`resolved()` and the constants it consults have to share one module; the
suite patches them here, and scripts/audit_seams.py fails if any reader
ever resolves them somewhere else.
"""
import dataclasses
import os
from dataclasses import dataclass

from comment_render import (  # comment rendering (same-dir module, stdlib only)
    _full_hunks_link,
    _group_repeated_sections,
    _name_list,
    _section_name,
    parse_diff_sections,
)
from envcfg import _env_int  # environment configuration readers (stdlib only)
from redact import (  # display-time redaction (same-dir module, stdlib only)
    _fence_safe,
    _redact_for_display,
    _redact_sensitive,
    _show_cr,
)


DISPLAY_BODY_MAX_CHARS = 6000  # v2.5.8: hard cap per resource body in the PR
                               # comment, WITH an explicit marker (protects
                               # the footer/status token from the blunt
                               # MAX_COMMENT_BYTES global cut)


# Proactive readability budget, far below the Bitbucket hard limit.
# Reality check on acme-config-prod (2026-08): 8 of the 14 most recent bot
# comments sat EXACTLY at the 245KB truncation wall — one of them a routine
# fleet-wide version bump (PR #3891) rendering 473 diff blocks. Meanwhile
# every committed "readable" golden comment is under 4KB. Nobody reads
# 150KB in a PR comment; the full-diff view exists for that. 30KB is ~8x
# the largest small-PR comment today and a few screen-scrolls at most.
# Critical panels (state flags, cause panel, decommission, VM changes,
# downgrades, deletions, renames) always render in full; only ordinary
# per-app diff blocks past this budget collapse into a pointer at the
# full-diff view. Env-overridable like the other capacity knobs.
COMMENT_READABLE_BYTES = _env_int("COMMENT_READABLE_BYTES", 30_000)


# Escape hatch for the uncapped full-diff page (COPS-2610): false restores
# the pre-2.33.0 page (bodies cut at DISPLAY_BODY_MAX_CHARS with a marker).
# ROLLBACK ORDER, once phase E (COPS-2612) is live: set
# COMMENT_INLINE_DIFFS=true FIRST, then flip this. The other order leaves
# the comment without YAML while the page truncates -- information is gone.
FULL_PAGE_UNCAPPED    = os.environ.get("FULL_PAGE_UNCAPPED", "true").strip().lower() in ("1", "true", "yes")
# ── Phase E switches (COPS-2612) ─────────────────────────────────────────
# The comment becomes a decision summary: verdicts, names and counts stay,
# the YAML evidence moves to the full-diff page. Safe only because phase C
# proved the page holds everything and retains it, and phase D made it
# navigable. Measured on the 36-PR corpus: median comment 9,874 bytes, of
# which 70-93% is YAML that the same reader can now open in one click.
#
# COMMENT_INLINE_DIFFS=true is the one-variable rollback to the old shape,
# and it is also the FIRST step when rolling back phase C -- see README.
# These are read at render time through RenderProfile.resolved(), never
# snapshotted at import, or the rollback would not work on a running pod.
COMMENT_INLINE_DIFFS  = os.environ.get("COMMENT_INLINE_DIFFS", "false").strip().lower() in ("1", "true", "yes")
COMMENT_INPUT_PANEL   = os.environ.get("COMMENT_INPUT_PANEL", "false").strip().lower() in ("1", "true", "yes")
# Narrow escape hatch: with inline diffs off, still show this many lines of
# evidence for BLOCK-severity findings only. 0 (default) means the comment
# ships with no fenced block at all.
COMMENT_INLINE_EVIDENCE_LINES = _env_int("COMMENT_INLINE_EVIDENCE_LINES", 0)


# COPS-2579: DiffResult.sections used to be hard-capped to
# AI_MAX_SECTIONS_PER_APP (10) at diff time -- a cap meant for the AI
# prompt (see the independent re-slice at generate_ai_summary's
# `sections[:AI_MAX_SECTIONS_PER_APP]`) that got reused as the STORAGE cap
# too, so any resource past #10 was discarded before the comment or the
# diff-UI page could ever show it (measured: 60 of 16616 sections shown on
# acme-config-prod PR #3837).
# COPS-2610 raised the bound again, 400 to 5,000. This is MEMORY safety,
# not display policy: the full list already exists in memory when
# _package_sections runs (every safety fact and the fingerprint are
# computed on it pre-cap), so the cap only bounds what is RETAINED in
# DiffResult across the whole run's app_results (the COPS-2543 OOM
# lesson). At 400 it silently amputated the artifact of any app past it --
# and the comment's note then claimed the remainder was "only in the full
# diff view", which was exactly where it was not. If an app ever exceeds
# 5,000, the trim is counted (section_cap_trims) and the FULL page says so
# instead of pretending completeness.
FULL_SECTIONS_MAX_PER_APP = _env_int("FULL_SECTIONS_MAX_PER_APP", 5000)


# ── Render profiles (COPS-2609, phase B of COPS-2607) ────────────────────
# This service renders two surfaces with one function: the PR comment and
# the full-diff page behind the ACME Diff Preview build status. Until now
# the only thing separating them was `readable_budget`, and that integer's
# truthiness had quietly accumulated four more jobs: whether byte-identical
# apps are grouped, whether version-transition noise folds, whether the
# overview table is row-capped, and whether the appendix collapses into a
# pointer. A call site could not read any of that, and the later phases of
# COPS-2607 need to say things the integer cannot express at all -- no body
# cap, no fences, no input panel.
#
# So the difference between the two surfaces becomes a value. The point is
# that phases C, D and E change a profile rather than the renderer.
@dataclass(frozen=True)
class RenderProfile:
    """How much of the truth a given surface shows.

    Frozen on purpose: these are module-level constants shared across every
    PR and every worker thread. A render able to mutate one would leak the
    change into unrelated PRs, and the symptom (a comment that folds
    differently depending on what was rendered before it) is close to
    impossible to reproduce. Use replace() for a variant.
    """
    name: str
    # Bulk-region readability budget in bytes. 0 renders everything.
    # None means "COMMENT_READABLE_BYTES, read at render time" -- see
    # resolved(). Do NOT bake the constant in as a default here: it is an
    # _env_int, so a snapshot taken at import silently ignores both the env
    # var and any runtime override, and the symptom is a comment that stops
    # honouring its own configured budget.
    readable_budget: int = None
    # Render the YAML hunks inline. Phase E (COPS-2612) turns this off for
    # the comment, which is only safe because phase C proved the page holds
    # everything and phase D made it navigable.
    # None = COMMENT_INLINE_DIFFS at render time. Never bake the env value
    # in as a default here, for the reason spelled out under
    # readable_budget: an import-time snapshot makes the switch decorative,
    # and this one is the phase E rollback.
    inline_diffs: bool = None
    # Render the "why this changed" input panel. Phase E moves it to the
    # page. None = COMMENT_INPUT_PANEL at render time.
    input_panel: bool = None
    # Collapse byte-identical apps into one representative (COPS-2579).
    # False on the FULL page (COPS-2679): format_comment honours this so
    # every environment keeps its own block and Index/#app- anchor.
    group_repeats: bool = True
    # Fold version-transition noise so needles stay visible (COPS-2606).
    version_fold: bool = True
    # Evidence lines to show per app when inline_diffs is off. Phase E
    # renders these for BLOCK-severity apps only, so a reviewer never has
    # to leave the comment to see WHY something is dangerous, only to see
    # the rest. None = COMMENT_INLINE_EVIDENCE_LINES at render time; 0 (the
    # default) means the comment ships with no fences at all.
    inline_evidence_lines: int = None
    # This surface IS the complete record, not a summary pointing at one
    # (COPS-2611). Two consequences, both about not lying to the reader:
    # it never renders a pointer to the full-diff page (it would be a link
    # to itself, and with no URL to hand it degrades to "the page could not
    # be produced" -- which the page then said about itself, live on 2.32.0
    # and 2.33.0), and when the storage cap trims an app it owns the
    # shortfall instead of directing the reader elsewhere.
    # A behaviour field rather than a check on name: a profile derived with
    # replace() under another name must keep behaving like the page.
    is_complete_record: bool = False
    # Hard cap per resource body. 0 = never cut (COPS-2610, the FULL page);
    # the sentinel is resolved through the FULL_PAGE_UNCAPPED hatch below.
    # None = DISPLAY_BODY_MAX_CHARS at render time (see readable_budget).
    body_max_chars: int = None
    # Sections kept per app. Applied when the diff is computed, not when it
    # is rendered, so phase C has to lift it there; carried here so the
    # value a surface wants is stated in one place.
    # None = FULL_SECTIONS_MAX_PER_APP at render time.
    section_cap: int = None

    def replace(self, **kw):
        return dataclasses.replace(self, **kw)

    def resolved(self):
        """Fill in every field that means "the module constant, right now".

        Called once per render. Idempotent, so a caller that hands in an
        already-resolved profile gets it back unchanged.

        body_max_chars: None means the module default; 0 means "never cut"
        and passes through the FULL_PAGE_UNCAPPED escape hatch, so flipping
        that env var back restores the pre-2.33.0 capped page without
        touching a profile (COPS-2610). Checked at render time for the same
        reason the constants are: an import-time snapshot would make the
        hatch decorative.
        """
        body_cap = self.body_max_chars
        if body_cap is None:
            body_cap = DISPLAY_BODY_MAX_CHARS
        elif body_cap == 0 and not FULL_PAGE_UNCAPPED:
            body_cap = DISPLAY_BODY_MAX_CHARS
        return dataclasses.replace(
            self,
            readable_budget=(COMMENT_READABLE_BYTES
                             if self.readable_budget is None
                             else self.readable_budget),
            inline_diffs=(COMMENT_INLINE_DIFFS
                          if self.inline_diffs is None
                          else self.inline_diffs),
            input_panel=(COMMENT_INPUT_PANEL
                         if self.input_panel is None
                         else self.input_panel),
            inline_evidence_lines=(COMMENT_INLINE_EVIDENCE_LINES
                                   if self.inline_evidence_lines is None
                                   else self.inline_evidence_lines),
            body_max_chars=body_cap,
            section_cap=(FULL_SECTIONS_MAX_PER_APP
                         if self.section_cap is None
                         else self.section_cap))

    @classmethod
    def from_readable_budget(cls, readable_budget):
        """Map the deprecated keyword onto a profile.

        None is the comment with the module default. 0 is the page: it is
        how process_pr and eleven existing tests ask for the complete
        render. Any other number is still the comment, just tighter -- the
        fold tests drive it with 8000/6000/2500 and must keep grouping and
        folding, which is exactly what the old `if budget:` did.
        """
        if readable_budget is None:
            return COMMENT_PROFILE
        if not readable_budget:
            return FULL_PROFILE
        return COMMENT_PROFILE.replace(readable_budget=readable_budget)


COMMENT_PROFILE = RenderProfile(name="COMMENT")
FULL_PROFILE = RenderProfile(
    name="FULL",
    readable_budget=0,
    group_repeats=False,
    version_fold=False,
    is_complete_record=True,
    # Pinned True, never resolved from the env: the page is the complete
    # record, so the phase E switches must not be able to empty it. Flipping
    # COMMENT_INLINE_DIFFS is a comment-shape decision; if it could also
    # reach the page, the rollback switch would delete the very thing it
    # exists to fall back on.
    inline_diffs=True,
    input_panel=True,
    inline_evidence_lines=0,
    # COPS-2610 (phase C): the page never cuts a resource body. Measured
    # before the change: acme-config-prod #3887's stored artifact carried
    # 981 "diff truncated for display" markers, 981 places where the
    # complete record told the reader to go somewhere else. 0 resolves
    # through the FULL_PAGE_UNCAPPED hatch, see resolved().
    body_max_chars=0,
    # section_cap stays live (None -> FULL_SECTIONS_MAX_PER_APP): the trim
    # happens at STORAGE time in _package_sections, shared by both
    # surfaces, and is a memory bound, not display policy. Raised to 5,000
    # and made loud in the same change.
)


def _format_app_diff_block(app, sections, diff_text, show_diff=True, n_res=None,
                           risk_headers=None, version_fold=None,
                           artifact_url="", size_budget=None,
                           group_repeats=False, profile=None,
                           row_pointer=True):
    """Return a list of markdown lines for one app's diff block.

    sections is DiffResult.sections — already truncated to display budget.
    n_res is the REAL total resource count (DiffResult.n_res); the header must
    report this, not len(sections), which is capped at AI_MAX_SECTIONS_PER_APP.
    Before v2.4.9 the header used len(sections), so an app that changed e.g.
    103 resources showed "10 resource(s) changed" and only 10 diffs, with no
    hint that 93 more changed silently (FIX B). show_diff=False outputs just
    the header line (large-mode table overflow).
    risk_headers (COPS-2567) is the set of deleted/zeroed headers for this app.
    When one of them is on display the sections are no longer a plain prefix,
    so the truncation note must not keep saying "first".
    Bitbucket does NOT render HTML <details>/<summary>, so we never use them.
    """
    profile = (profile or COMMENT_PROFILE).resolved()
    shown = len(sections) if sections else 0
    total = n_res if n_res is not None else shown
    n = total if total else 1
    out = [f"\u26a0\ufe0f **`{app}`** \u2014 {n} resource(s) changed", ""]
    if not show_diff:
        return out
    # profile.inline_diffs (COPS-2609): the mechanism phase E flips. Both
    # profiles keep it on today, so this branch is inert -- it exists so E
    # is a profile change rather than another surgery on this function.
    # What it must never do is drop the app: the header above still names
    # it and states how many resources changed, and the pointer says where
    # the hunks are. That is a relocation, not a loss.
    # Version-transition fold. Computed BEFORE the inline_diffs gate below,
    # because its summary line is a CONCLUSION, not evidence: "6 of 7
    # changed resources are the version transition 2602 -> 2603 only" is
    # exactly the sentence a reviewer needs to decide, and it contains no
    # YAML. Phase E moves the hunks to the page; dropping this line with
    # them would break umbrella rule 2 (never lose information) while
    # claiming to only relocate it.
    folded = set()
    _fold_lines = []
    if version_fold and version_fold.get("n_foldable"):
        folded = set(version_fold.get("headers") or ())
        _lbl = version_fold.get("label")
        _are = (f"are the version transition `{_lbl}` only"
                if _lbl else "are version-only updates")
        # Two short paragraphs inside the quote, never one long line: a
        # prose wall past ~350 chars wraps into something nobody reads
        # (measured on the last 50 merged prod comments, COPS-2605).
        # The link is appended here only when the hunks stay inline. With
        # phase E the block emits its own pointer immediately below, and
        # the same URL twice in adjacent lines reads like a rendering bug.
        _fold_link = (f" {_full_hunks_link(artifact_url, app=app)}"
                      if profile.inline_diffs else "")
        _fold_lines += [f"> \u2b06\ufe0f **{version_fold['n_foldable']} of "
                        f"{total} changed resource(s)** {_are}.{_fold_link}"]
        _what = ", ".join(version_fold.get("classes") or ())
        if _what:
            _fold_lines += [">", f"> Folded lines: {_what}."]
        # Name the needles. The fold line says "6 of 7 are version-only",
        # which leaves the reader knowing one resource changed for another
        # reason and not which one. With the hunks inline that was answered
        # by reading on; with phase E moving them to the page it would not
        # be answered at all, so the names come up into the comment. Names
        # are not evidence, and umbrella rule 2 is about information, not
        # about bytes.
        _needles = [h for h, _ in (sections or []) if h not in folded]
        if _needles and not profile.inline_diffs:
            _shown = ", ".join(f"`{_section_name(h)}`" for h in _needles[:5])
            _extra = (f" *(+{len(_needles) - 5} more)*"
                      if len(_needles) > 5 else "")
            _fold_lines += [">", f"> Changed for another reason: "
                                 f"{_shown}{_extra}"]
        _fold_lines += [""]
    if not profile.inline_diffs:
        # COPS-2612: the block keeps its header (the app and its REAL
        # resource count) and points at the page. Narrow exception: with
        # COMMENT_INLINE_EVIDENCE_LINES > 0, a risk-flagged app still shows
        # that many lines from its offending resources, so a reviewer never
        # has to leave the comment to see WHY something is dangerous -- only
        # to see the rest. Routine apps stay fence-free.
        out += _fold_lines
        _n = profile.inline_evidence_lines
        if _n and risk_headers:
            for hdr, body in (sections or []):
                if hdr not in risk_headers:
                    continue
                _ev = _redact_for_display(hdr, _show_cr(body)).rstrip()
                _ev = "\n".join(_ev.split("\n")[:_n])
                out += [f"**`{_fence_safe(hdr)}`**", "", "```diff",
                        _fence_safe(_ev), "```", ""]
        if row_pointer:
            return out + [_full_hunks_link(artifact_url, app=app), ""]
        # COPS-2640: the app's Changeset overview row already carries this
        # exact link, and Bitbucket rendered the duplicate broken anyway
        # (link text "Full hunks for " with the app name outside the
        # anchor, audited on acme-config-prod #4095). The conclusions
        # above are the block's value; the pointer is the row's job.
        return out + [""]
    out += _fold_lines
    inline = [(h, b) for h, b in (sections or []) if h not in folded]
    # Identical changes collapse to one hunk plus a count. Off on the
    # full-diff page (the caller leaves group_repeats False there), which
    # must stay the complete record.
    dups = {}
    if group_repeats:
        inline, dups = _group_repeated_sections(inline, risk_headers)
    if sections:
        if total > shown:
            # COPS-2567: only claim a plain prefix when it still is one.
            # COPS-2610: total > shown means the STORAGE cap trimmed this
            # app -- the remainder is gone from both surfaces, so on the
            # FULL page (the self-described complete record) the note must
            # own the shortfall instead of pointing at itself.
            if not profile.inline_diffs or profile.is_complete_record:
                note = (f"> \u26a0\ufe0f **Storage cap reached: showing "
                        f"{shown} of {total} changed resources.** The "
                        f"remainder was not retained "
                        f"(FULL_SECTIONS_MAX_PER_APP). See ArgoCD for the "
                        f"live state.")
            elif folded:
                # With the fold active "showing first N of M" would be
                # false; say what the storage cap left out instead.
                note = (f"> \U0001f50d {total - shown} more changed "
                        f"resource(s) beyond the storage cap are only in "
                        f"the full diff view.")
            else:
                n_risk = sum(1 for hdr, _ in sections
                             if hdr in (risk_headers or ()))
                if n_risk:
                    note = (f"> \U0001f50d Showing {shown} of {total} changed "
                            f"resources, the {n_risk} highest-risk one(s) "
                            f"first. See ArgoCD for the full set.")
                else:
                    note = (f"> \U0001f50d Showing first {shown} of {total} "
                            f"changed resources. See ArgoCD for the full set.")
            out += [note, ""]
        omitted = []
        used = sum(len(_l.encode("utf-8")) + 1 for _l in out)
        rendered_one = False
        for hdr, body in inline:
            if (size_budget is not None and rendered_one
                    and hdr not in (risk_headers or ())
                    and used > size_budget):
                # Intra-app readable budget (see format_comment): ordinary
                # sections past the budget fold into one pointer below.
                # Risk sections are exempt, and the first section always
                # renders so the block is never headline-only.
                omitted.append(hdr)
                omitted.extend(dups.get(hdr) or ())
                continue
            # Redaction happens here, at display time, so the diff engine
            # still compares real values and detects Secret changes.
            # v2.5.8: sections bodies are NOT pre-truncated (only
            # DiffResult.text is) — this docstring used to claim otherwise.
            # A single giant resource diff (huge ConfigMap rewrite) could
            # push the whole comment past MAX_COMMENT_BYTES, whose blunt
            # global cut chops off the footer and its status token. Cap
            # each body here WITH an explicit marker. Redact BEFORE the
            # cut so truncation can never split a value a redaction rule
            # would have caught.
            # v2.5.19 E3: make CR visible BEFORE redaction — the redaction
            # helpers use splitlines(), which silently eats a trailing \r, so
            # a CRLF<->LF-only change would otherwise collapse to "no visible
            # change". Convert first, then redact, then neutralize fences.
            body_disp = _redact_for_display(hdr, _show_cr(body)).rstrip()
            # profile.body_max_chars (COPS-2609/2610): a COMMENT protection
            # (one giant ConfigMap rewrite must not push the comment past
            # MAX_COMMENT_BYTES and chop the status token off). 0 = never
            # cut: the FULL page is the complete record and a cut there is
            # a lie, not a protection.
            if profile.body_max_chars and \
                    len(body_disp) > profile.body_max_chars:
                body_disp = (body_disp[:profile.body_max_chars].rstrip()
                             + "\n... (diff truncated for display \u2014 see "
                               "ArgoCD for the full resource diff)")
            body_disp = _fence_safe(body_disp)
            chunk = [f"**`{_fence_safe(hdr)}`**", "", "```diff", body_disp,
                     "```", ""]
            _same = dups.get(hdr) or []
            if _same:
                chunk += [f"> \u267b\ufe0f **{len(_same)} more resource(s) "
                          f"change exactly the same lines.**"]
                _also = _name_list(_same)
                if _also:
                    chunk += [">", f"> Same change: {_also}"]
                chunk += [""]
            for _l in chunk:
                used += len(_l.encode("utf-8")) + 1
            out += chunk
            rendered_one = True
        if omitted:
            out += [f"> \u2702\ufe0f **{len(omitted)} more changed "
                    f"resource(s) omitted** here to keep the comment "
                    f"scannable. None is a deletion, zeroed replica or "
                    f"VM change. {_full_hunks_link(artifact_url, app=app)}"]
            _names = _name_list(omitted)
            if _names:
                out += [">", f"> Omitted: {_names}"]
            out += [""]
    elif diff_text:
        # v2.5.17: this fallback (sections not supplied -- reachable through
        # _result()'s legacy 3-tuple coercion, which rebuilds sections with
        # parse_diff_sections() but skips _filter_diff_sections(), and can
        # end up with an empty section list for non-empty text) used to run
        # only the flat _redact_sensitive() pass. That pass is not kind-aware
        # and only catches keys matching _SENSITIVE_KEYS, so a `kind: Secret`
        # body reaching this branch was never whole-masked, and any Secret
        # data key not in that list (tls.crt, ca.bundle, .dockerconfigjson,
        # ...) leaked verbatim. Confirmed live with a probe. Not reachable
        # through the real diff pipeline today (argocd_diff always keeps
        # diff_text and sections in lockstep), but a real landmine for the
        # legacy path or a future refactor that breaks that invariant.
        #
        # Fix: recover the same (hdr, body) sections the primary path above
        # would have had and redact each one the same way. Only fall back
        # further to the flat pass when the text has no "===== hdr ====="
        # markers at all to key off (truly unstructured legacy diff text).
        legacy_secs = parse_diff_sections(diff_text)
        if legacy_secs:
            redacted = "\n".join(
                f"===== {hdr} =====\n{_redact_for_display(hdr, _show_cr(body)).rstrip()}"
                for hdr, body in legacy_secs
            )
        else:
            redacted = _redact_sensitive(_show_cr(diff_text)).rstrip()
        out += ["```diff", _fence_safe(redacted), "```", ""]
    return out
