"""The full-diff page must be navigable (COPS-2611, phase D).

Phase C made the page complete. Complete and unnavigable is not usable: the
measured corpus renders 345 applications / 19,869 resources (prod #3887) and
774 applications / 11,086 resources (#3890), and after phase E this page is
the only place a reviewer can inspect what changed. Finding
`ComputeInstance pv-bos-svc-a` in 786,150 rows with the browser's own find
is not a review.

Two properties the tests below defend hardest, because both are ways this
change could quietly make things worse rather than better:

- **No line is lost to the parser.** The index is derived from markers the
  comment renderer already emits. A body that does not match them must
  still render exactly as it does today, line for line. A parser that
  silently swallows an unrecognised heading would trade a navigable page
  for an incomplete one, which is the opposite of phases C and D.
- **Nothing escapes escaping.** Every value here is PR-controlled: an app
  name, a namespace and a resource name all come from a branch someone
  pushed. They now reach the DOM in three new places (index label, anchor
  id, filter attribute) instead of one.
"""
import os
import re
import sys

import pytest

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diff_ui as ui

SHA = "a2e6383e7c1d4f5a6b7c8d9e0f1a2b3c4d5e6f70"
BT = chr(96)
FENCE = BT * 3


def _app(name, resources):
    """One application block in the shape format_comment emits."""
    out = [f"\u26a0\ufe0f **{BT}{name}{BT}** \u2014 {len(resources)} "
           f"resource(s) changed", ""]
    for key in resources:
        out += [f"**{BT}{key}{BT}**", "", f"{FENCE}diff",
                "-  replicas: 2", "+  replicas: 3", FENCE, ""]
    return out


def _body(*apps):
    head = ["## \U0001f52d ACME Diff Preview", "",
            "### \U0001f9ed Merge summary", "",
            "\u2705 **Routine**", ""]
    for a in apps:
        head += a
    return "\n".join(head)


def _render(body, **kw):
    return ui.render_html({"repo": "acme-config-prod", "pr_id": 3899,
                           "sha": SHA, "body": body}, **kw)


THREE = _body(
    _app("pv-alpha-a-ms", ["/apps/Deployment web", "/Service web",
                           "/ConfigMap web-config"]),
    _app("pv-beta-b-ss", ["/apps/StatefulSet db", "/Service db",
                          "/v1/Secret db-credentials"]),
    _app("pv-gamma-c-glb", ["/compute.cnrm.cloud.google.com/ComputeInstance "
                            "pv-gamma/vm-a", "/Service lb",
                            "/networking.k8s.io/Ingress lb"]),
)


# --- 1. the index reflects what is on the page --------------------------

def test_three_apps_and_nine_resources_are_indexed():
    page = _render(THREE)
    outline = ui.build_outline(THREE)
    assert len(outline) == 3
    assert sum(len(a["resources"]) for a in outline) == 9
    for app in ("pv-alpha-a-ms", "pv-beta-b-ss", "pv-gamma-c-glb"):
        assert app in page
    assert "vm-a" in page


def test_the_index_states_the_totals_before_you_scroll():
    page = _render(THREE)
    assert re.search(r"3\s+application", page)
    assert re.search(r"9\s+resource", page)


# --- 2. every index target exists, exactly once -------------------------

def test_every_index_link_lands_somewhere_and_lands_once():
    page = _render(THREE)
    targets = set(re.findall(r'href="#([a-z0-9-]+)"', page))
    assert targets, "the index must link to anchors"
    for t in targets:
        hits = len(re.findall(r'id="%s"' % re.escape(t), page))
        assert hits == 1, f"anchor {t!r} appears {hits} times, expected once"


def test_the_same_resource_name_in_two_apps_gets_two_anchors():
    """Resource names repeat across environments constantly (`/Service web`
    in every app). Anchors are scoped by app or a deep link sends the
    reader to whichever one happened to render first."""
    body = _body(_app("pv-one-a-ms", ["/Service web"]),
                 _app("pv-two-b-ms", ["/Service web"]))
    outline = ui.build_outline(body)
    ids = [r["id"] for a in outline for r in a["resources"]]
    assert len(ids) == 2 and len(set(ids)) == 2


# --- 3. anchors are stable ----------------------------------------------

def test_anchors_are_stable_across_renders():
    """A deep link pasted into a ticket has to survive the next commit
    that leaves that resource untouched."""
    a = [r["id"] for app in ui.build_outline(THREE) for r in app["resources"]]
    b = [r["id"] for app in ui.build_outline(THREE) for r in app["resources"]]
    assert a == b


def test_anchor_ids_are_url_safe():
    ids = [app["id"] for app in ui.build_outline(THREE)]
    ids += [r["id"] for app in ui.build_outline(THREE)
            for r in app["resources"]]
    for i in ids:
        assert re.fullmatch(r"[a-z0-9-]+", i), f"unsafe anchor id: {i!r}"


# --- 4. PR-controlled content cannot break out --------------------------

HOSTILE_APP = 'pv-evil"><script>alert(1)</script>-ms'
HOSTILE_RES = '/apps/Deployment "><img src=x onerror=alert(2)>'


def test_a_hostile_name_is_escaped_in_body_index_and_anchor():
    """Three new places take PR-controlled strings: the index label, the
    anchor id, and the filter attribute. All three are on the same page as
    the escaped body, so one miss is a stored XSS behind IAP.

    Asserted as "no live markup", not "the payload substring is absent":
    `onerror=alert(2)` contains nothing html.escape touches, so it survives
    as inert text inside an escaped cell and that is correct. What must
    never survive is an unescaped tag or a quote that closes an attribute.
    """
    page = _render(_body(_app(HOSTILE_APP, [HOSTILE_RES])))
    # The page ships exactly one script of its own (theme switcher + index
    # filter). Anything beyond that count came from the body.
    assert page.lower().count("<script") == 1, "an extra <script> appeared"
    outside = re.sub(r"(?is)<script.*?</script>", "", page)
    for live in ("<script", "<img", "<b>"):
        assert live not in outside.lower(), \
            f"unescaped markup survived: {live}"
    assert "&lt;script&gt;" in page, "the payload must survive as inert text"
    # nothing hostile survived into an id=, href= or data-k= attribute
    for pat in (r'id="([^"]*)"', r'href="#([^"]*)"', r'data-k="([^"]*)"'):
        for attr in re.findall(pat, page):
            assert "<" not in attr and ">" not in attr


def test_hostile_names_still_produce_a_usable_anchor():
    """Sanitising must not collapse every hostile name to the same id, or
    two resources would share one anchor."""
    outline = ui.build_outline(_body(
        _app(HOSTILE_APP, [HOSTILE_RES, '/Service <b>x</b>'])))
    ids = [r["id"] for r in outline[0]["resources"]]
    assert len(ids) == len(set(ids)) == 2
    for i in ids:
        assert re.fullmatch(r"[a-z0-9-]+", i)


# --- 5. the parser may never cost a line --------------------------------

def test_every_source_line_still_renders():
    """The one way this change could be worse than no change at all."""
    for body in (THREE, "no structure at all\njust two lines",
                 "", f"{FENCE}diff\n+ a\n{FENCE}",
                 "\u26a0\ufe0f **`half-a-header`** \u2014 malformed"):
        page = _render(body)
        assert page.count("<tr") >= len(body.split("\n"))


def test_an_unparseable_body_renders_with_an_empty_index():
    page = _render("just a wall of text\nwith no markers")
    assert ui.build_outline("just a wall of text") == []
    assert "just a wall of text" in page


def test_a_body_with_no_apps_keeps_the_panels():
    body = "\n".join(["## \U0001f52d ACME Diff Preview", "",
                      "### \U0001f9ed Merge summary", "", "\u2705 Routine"])
    page = _render(body)
    assert "Merge summary" in page


# --- 6. no new dependency -----------------------------------------------

def test_the_page_still_ships_zero_external_assets():
    """The page has always been self-contained: no CDN, no font, no
    framework. It is served behind IAP to reviewers on arbitrary networks,
    and a filter box is not a reason to start making outbound requests."""
    page = _render(THREE)
    for bad in ("http://", "https://cdn", "<link", "integrity=",
                "googleapis.com/css", "unpkg", "jsdelivr"):
        assert bad not in page.replace('href="#', ""), f"external asset: {bad}"


def test_the_filter_never_uses_innerhtml_on_body_derived_text():
    page = _render(THREE)
    assert "innerHTML" not in page


# --- 7. what phase C added must still be there --------------------------

def test_the_sha_mismatch_notice_survives_the_new_index():
    page = _render(THREE, requested_sha="deadbeefcafe")
    assert "you requested" in page
    assert "deadbeefcafe" in page
    assert ui.build_outline(THREE), "index still present alongside it"


def test_raw_is_untouched_by_this_phase(tmp_path):
    ui.save_artifact(str(tmp_path), "acme-config-prod", 3899, SHA, THREE)
    code, ctype, payload = ui.respond(
        f"/diff/acme-config-prod/3899/{SHA}/raw", str(tmp_path), True)
    assert code == 200
    assert payload == THREE.encode("utf-8")
    assert "text/plain" in ctype


def test_the_retention_page_is_untouched(tmp_path):
    code, ctype, payload = ui.respond(
        f"/diff/acme-config-prod/4242/{SHA}", str(tmp_path), True)
    assert code == 404
    assert "no longer retained" in payload.decode()


def test_an_anchor_inside_the_collapsed_overflow_is_still_reachable():
    """The index is worth most on exactly the pages where most rows sit in
    the collapsed overflow. Without the reveal-on-hash handler, clicking
    such an entry does nothing at all: the target exists but is inside a
    hidden tbody.

    Asserted at the source level (no browser here), so it is a smoke test:
    it proves the handler ships and is wired to both hashchange and load,
    not that a real browser scrolled. The live check is in the ticket.
    """
    n = ui.MAX_VISIBLE_LINES + 50
    filler = "\n".join(f"note line {i}" for i in range(n))
    # the app block goes AFTER the filler, so its anchor lands in the
    # collapsed overflow rather than in the visible rows
    body = filler + "\n" + _body(_app("pv-tail-a-ms", ["/Service late"]))
    page = _render(body)
    assert 'class="rest" hidden' in page, "fixture must produce an overflow"
    outline = ui.build_outline(body)
    target = outline[0]["resources"][0]["id"]
    assert f'id="{target}"' in page
    assert "hashchange" in page
    assert "revealTarget" in page
