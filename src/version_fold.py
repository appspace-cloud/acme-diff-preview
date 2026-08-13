"""Version-transition fold: which sections are provably a version bump.

Sliced out of diff_preview.py unchanged (COPS-2658 phase 3). A leaf by
construction: it reads nothing but `re` and its own names, so there is no
module it could import the service back through.

The classifier answers one question -- is this section provably nothing but
a version bump? -- and the safe failure direction is always "no". Anything
unpaired, unknown or ambiguous keeps its section inline, because a false
positive here folds a real change behind a one-line summary.
"""
import re


# ── Version-transition fold ───────────────────────────────────────────
# A platform version bump renders as dozens or hundreds of near-identical
# sections (185 and 473 on two real acme-config-prod PRs) whose ONLY
# changed lines are image tags, chart/version labels, checksum
# annotations, version-carrying env values and deploy timestamps.
# Inlining them all is what pushed single-environment bump comments to
# the 245KB hard cap, and it buried the one real change a reviewer
# needed to see (a brand-new KCC reconcile-interval annotation, found
# live inside 473 sections of noise). The classifier below decides,
# deterministically, which sections are provably that noise so the
# comment can fold them behind one line. The safe failure direction is
# always "keep the section inline": anything unpaired, unknown or
# ambiguous makes the whole section a needle.
_VERSION_FOLD_MIN = 3          # a fold line only pays for itself at 3+
_FOLD_CHECKSUM_RE = re.compile(r"^checksum/[\w./-]+$")
_FOLD_HEX_RE      = re.compile(r"^[0-9a-f]{6,64}$")
_FOLD_ISO_TS_RE   = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
# Keys whose value IS a version (or carries one as a -<version> suffix).
# Deliberately narrow: a generic "version:" config key must NOT be here,
# it can mean anything (schema versions, API versions, file formats).
_FOLD_CHART_LABEL_KEYS = ("helm.sh/chart", "app.kubernetes.io/version",
                          "appVersion", "targetRevision")
_FOLD_TRAILING_VER_RE  = re.compile(r"^(.*?)-(\d[\w.+-]*)$")
_FOLD_CLASS_ORDER = ("image tags", "chart labels", "version env values",
                     "checksums", "deploy timestamps")


def _fold_pairs(body: str):
    """Pair every changed line of a unified-diff body by YAML key.

    Returns a list of (key, old_value, new_value) or None when the body
    cannot be fully paired: a pure addition or deletion, an unbalanced
    key, or any changed line that is not a simple "key: value". Pairing
    is positional per key (the i-th removed "value:" matches the i-th
    added one); if a hunk reorders lines the values will not classify
    and the section stays inline, which is the safe direction.
    """
    minus, plus = [], []
    for raw in body.splitlines():
        if raw.startswith(("---", "+++", "@@", "\\")):
            continue
        if raw[:1] == "-":
            minus.append(raw[1:])
        elif raw[:1] == "+":
            plus.append(raw[1:])
    if not minus or not plus:
        return None

    def _kv(sline):
        t = sline.strip()
        if t.startswith("- "):
            t = t[2:]
        if ":" not in t:
            return None
        k, v = t.split(":", 1)
        return k.strip(), v.strip().strip('"').strip("'")

    by_key_old, by_key_new = {}, {}
    for src, sink in ((minus, by_key_old), (plus, by_key_new)):
        for sline in src:
            kv = _kv(sline)
            if kv is None:
                return None
            sink.setdefault(kv[0], []).append(kv[1])
    if set(by_key_old) != set(by_key_new):
        return None
    pairs = []
    for k, olds in by_key_old.items():
        news = by_key_new[k]
        if len(olds) != len(news):
            return None
        pairs.extend((k, o, n) for o, n in zip(olds, news))
    return pairs


def _split_image(v: str):
    """('repo', 'tag') for repo:tag image references, else (None, None)."""
    if ":" not in v:
        return None, None
    repo, tag = v.rsplit(":", 1)
    if not repo or not tag or "/" in tag:
        return None, None
    return repo, tag


def _classify_fold_pair(key, old, new, candidates):
    """One paired change -> (noise_class, version_pair) or (None, None).

    candidates is the set of (old, new) version transitions observed on
    unambiguous carriers; a bare env "value:" pair is only accepted when
    it repeats one of those, so an unrelated config value can never fold.
    """
    if _FOLD_CHECKSUM_RE.match(key):
        if (_FOLD_HEX_RE.match(old) and _FOLD_HEX_RE.match(new)
                and old != new):
            return "checksums", None
        return None, None
    kl = key.lower()
    if kl == "image" or kl.endswith("image"):
        ro, to = _split_image(old)
        rn, tn = _split_image(new)
        if ro and ro == rn and to != tn:
            candidates.add((to, tn))
            return "image tags", (to, tn)
        return None, None
    if key in _FOLD_CHART_LABEL_KEYS:
        if key == "helm.sh/chart":
            mo = _FOLD_TRAILING_VER_RE.match(old)
            mn = _FOLD_TRAILING_VER_RE.match(new)
            if (mo and mn and mo.group(1) == mn.group(1)
                    and mo.group(2) != mn.group(2)):
                pair = (mo.group(2), mn.group(2))
                candidates.add(pair)
                return "chart labels", pair
            return None, None
        if old != new:
            candidates.add((old, new))
            return "chart labels", (old, new)
        return None, None
    if key == "value":
        if _FOLD_ISO_TS_RE.match(old) and _FOLD_ISO_TS_RE.match(new):
            return "deploy timestamps", None
        if (old, new) in candidates:
            return "version env values", (old, new)
        return None, None
    return None, None


def _classify_version_fold(sections, version_change=None,
                           exempt=frozenset()):
    """Which sections are provably version-bump noise, and which are not.

    Two passes over the paired changes. The first collects the version
    transitions this app is taking from unambiguous carriers (image
    tags, chart labels, targetRevision, plus the app-level chart
    version_change when known). The second accepts a section only when
    EVERY changed line pairs up and classifies against that vocabulary.
    Returns None when fewer than _VERSION_FOLD_MIN sections fold (a fold
    line for one or two sections costs more attention than it saves),
    else a dict with the fold facts for the comment renderer.
    """
    if not sections:
        return None
    candidates = set()
    if (version_change and version_change[0] and version_change[1]
            and version_change[0] != version_change[1]):
        candidates.add((str(version_change[0]), str(version_change[1])))
    paired = []
    for hdr, body in sections:
        pairs = None if hdr in exempt else _fold_pairs(body)
        paired.append((hdr, pairs))
        if pairs:
            for key, old, new in pairs:
                _classify_fold_pair(key, old, new, candidates)
    foldable_headers, classes = [], set()
    chart_votes, other_votes = {}, {}
    for hdr, pairs in paired:
        if not pairs:
            continue
        ok, sec_classes, sec_votes = True, set(), []
        for key, old, new in pairs:
            cls, pair = _classify_fold_pair(key, old, new, candidates)
            if cls is None:
                ok = False
                break
            sec_classes.add(cls)
            if pair:
                sec_votes.append((cls, pair))
        if not ok:
            continue
        foldable_headers.append(hdr)
        classes |= sec_classes
        for cls, pair in sec_votes:
            book = chart_votes if cls == "chart labels" else other_votes
            book[pair] = book.get(pair, 0) + 1
    if len(foldable_headers) < _VERSION_FOLD_MIN:
        return None
    votes = chart_votes or other_votes
    label = None
    if votes:
        old, new = max(votes.items(), key=lambda kv: kv[1])[0]
        label = f"{old} \u2192 {new}"
    return {"n_foldable": len(foldable_headers),
            "n_total": len(sections),
            "label": label,
            "headers": tuple(foldable_headers),
            "classes": tuple(c for c in _FOLD_CLASS_ORDER if c in classes)}
