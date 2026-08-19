"""COPS-2697: shared user-content bucket / DNS identity across environments.

Why this exists. During a migration an environment is cloned under a new
instance suffix (`pv-<cust>-a` -> `pv-<cust>-b`), traffic is cut over, and the
old one is decommissioned later. The user-content **bucket** and its **DNS A
record** are NOT built from `appspace.suffix` — they are built from
`buckets.userContent.suffix`, which every regional `config.yaml` sets to the
same value for every environment in that cluster. So the old and the new
environment resolve to the SAME GCP objects, and decommissioning the old one
with the data purge armed deletes a bucket and an A record the surviving
environment is still serving from. AE-15284 was a Sev1 of exactly this shape
on `pv-toronto-a`.

Identity is mirrored from the load-balancer chart, verified byte-identical
across every chart version live in production (2603.0.19, 2603.1.23, 2603.2.1,
2601.4.19):

  bucket   `helm-charts/load-balancer/templates/gcp/shared/user-content/
            compute-storagebucket-usercontent.yaml`
           + `templates/tpl/_helpers.tpl` -> appspace.userContentBucketFullName
      {prefix}-{customerName}-{name}-{REGION_KEY}-{suffix}.{domain}

  DNS      `.../compute-dnsrecordset-usercontent.yaml` -> $dnsName
      {prefix}-{customerName}-{name}-{REGION_KEY}-{suffix}.{managedZone.domain}.

Three details the chart makes explicit and a naive reading gets wrong:

  * REGION_KEY is the **map key** of `regionMapping`, not `regionMapping.<k>.region`
    (that GCP region name is only the bucket's `location`). One object is
    rendered per key, so an environment can own SEVERAL buckets and FQDNs.
  * the bucket uses `userContent.domain`, the record uses
    `userContent.managedZone.domain` — different domains, and the record also
    requires `managedZone.enabled`.
  * both are gated on `userContent.enabled` AND
    (`coreTypeName == "user-content"` OR `coreTypeName` empty).

`name`, `enabled`, `suffix` and `lbSuffix` are **chart defaults**, not config
repo values — the config tree overrides `suffix`, `domain`, `regionMapping`
and `managedZone`, but never sets `name`. Defaults below are read from
`helm-charts/load-balancer/values.yaml`; they are applied as the base layer so
this module needs no chart render and no GCP call. They cancel out for the
comparison itself (both sides get the same base), so a drifted default can
only make a rendered NAME wrong in the message, never invent or hide a match.
"""

# helm-charts/load-balancer/values.yaml @ 2603.0.19, appspace.buckets.userContent.
# Only fields the config tree never sets belong here. `managedZone.enabled` is
# the one that matters most: the chart default is TRUE and no regional
# config.yaml sets it (they override only name/domain/project), so defaulting
# it to false would silently switch the whole DNS half of this guard off —
# which is precisely the AE-15284 failure shape.
_CHART_DEFAULTS = {
    "name": "content",
    "enabled": True,
    "suffix": "a",
    "managedZone.enabled": True,
}

_UC = "appspace.buckets.userContent."


def _truthy(v, default=False) -> bool:
    """YAML gives bools; config overrides sometimes arrive as strings."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _val(flat: dict, leaf: str, default=None):
    v = flat.get(_UC + leaf)
    if v is None:
        return _CHART_DEFAULTS.get(leaf, default)
    return v


def region_keys(flat: dict) -> list:
    """Map keys of `regionMapping`, which is what the chart ranges over.

    Recovered from the flattened chain: `...regionMapping.<KEY>.<field>`.
    Returned sorted so rendered messages are deterministic.
    """
    prefix = _UC + "regionMapping."
    keys = set()
    for k in flat:
        if not k.startswith(prefix):
            continue
        rest = k[len(prefix):]
        if "." in rest:
            keys.add(rest.split(".", 1)[0])
    return sorted(keys)


def identity(flat) -> dict:
    """User-content objects an environment owns, from its merged value chain.

    Returns a dict with:
      proven   False when identity could not be computed. Callers MUST treat
               an unproven identity as a possible sharer (fail closed), the
               same convention `_merged_kcc_flat_for_env` returning None uses
               for the VM checks (COPS-2683).
      buckets  set of bucket names  (empty when the chart renders none)
      fqdns    set of DNS FQDNs, trailing dot included, as the record carries
      reason   short human string when not proven, for the log

    An environment with `enabled: false`, a non-user-content `coreTypeName`,
    or no `regionMapping` renders nothing: it owns no objects, so it can
    neither lose data nor be a sharer. That is `proven` with empty sets, NOT
    unproven — a definite "owns nothing" must not read as "might share".
    """
    if flat is None:
        return {"proven": False, "buckets": set(), "fqdns": set(),
                "reason": "value chain unreadable"}

    core = str(flat.get("appspace.coreTypeName") or "").strip()
    if core and core != "user-content":
        return {"proven": True, "buckets": set(), "fqdns": set(), "reason": ""}

    if not _truthy(_val(flat, "enabled"), default=True):
        return {"proven": True, "buckets": set(), "fqdns": set(), "reason": ""}

    # regionMapping is what the chart ranges over, so it decides existence and
    # must be checked FIRST. No keys means no StorageBucket and no
    # DNSRecordSet are rendered at all, and that is a definite "owns nothing"
    # even when the rest of the block is absent too - which is the normal
    # shape for every environment that is not a user-content GLB. Testing
    # prefix/customerName before this made those environments "unproven",
    # which turned every ordinary armed-decommission panel into a warning.
    rkeys = region_keys(flat)
    if not rkeys:
        return {"proven": True, "buckets": set(), "fqdns": set(), "reason": ""}

    prefix = flat.get("appspace.prefix")
    customer = flat.get("appspace.customerName")
    if not prefix or not customer:
        # The chart `required`s both, so an environment that really renders a
        # bucket always has them. Missing them while regionMapping IS present
        # means our merge did not see the whole chain -> fail closed rather
        # than silently owning nothing.
        return {"proven": False, "buckets": set(), "fqdns": set(),
                "reason": "appspace.prefix or appspace.customerName absent"}

    name = _val(flat, "name")
    suffix = _val(flat, "suffix")
    # domain and managedZone.domain carry identity, and every real regional
    # config.yaml sets both. We deliberately do NOT fall back to their chart
    # defaults (a dev.* domain): if they are absent our view of the chain is
    # partial, and a defaulted domain would compute a name that cannot match
    # the sibling's real one — a MISSED match, the one direction that loses
    # customer data. Absent -> unproven, below.
    domain = flat.get(_UC + "domain")
    mz_on = _truthy(_val(flat, "managedZone.enabled"), default=True)
    mz_domain = flat.get(_UC + "managedZone.domain")

    buckets, fqdns = set(), set()
    for rk in rkeys:
        stem = f"{prefix}-{customer}-{name}-{rk}-{suffix}"
        if domain:
            buckets.add(f"{stem}.{domain}")
        if mz_on and mz_domain:
            fqdns.add(f"{stem}.{mz_domain}.")
    # regionMapping present but a domain missing is a real gap, not "owns
    # nothing": the chart `required`s them, so our view of the chain is
    # partial and any comparison from it could miss a live sharer.
    if not domain:
        return {"proven": False, "buckets": buckets, "fqdns": fqdns,
                "reason": "userContent.domain absent from the merged chain"}
    if mz_on and not mz_domain:
        return {"proven": False, "buckets": buckets, "fqdns": fqdns,
                "reason": "userContent.managedZone.domain absent from the "
                          "merged chain"}
    return {"proven": True, "buckets": buckets, "fqdns": fqdns, "reason": ""}


def shared_owners(target: dict, siblings: dict) -> dict:
    """Which surviving environments share the target's bucket names or FQDNs.

    target    identity() of the environment being torn down
    siblings  {env_label: identity()} for every surviving candidate

    Returns {env_label: {"buckets": sorted[...], "fqdns": sorted[...],
                         "unproven": bool}} for the sharers only.

    A sibling whose identity is unproven is reported with `unproven: True` and
    no names: we cannot show that it does NOT share, and a teardown that may
    delete live customer data must not proceed on an unread file.
    """
    out = {}
    tb, tf = target.get("buckets") or set(), target.get("fqdns") or set()
    if not tb and not tf:
        return out
    for label, ident in sorted((siblings or {}).items()):
        if not ident.get("proven"):
            out[label] = {"buckets": [], "fqdns": [], "unproven": True}
            continue
        b = sorted(tb & (ident.get("buckets") or set()))
        f = sorted(tf & (ident.get("fqdns") or set()))
        if b or f:
            out[label] = {"buckets": b, "fqdns": f, "unproven": False}
    return out
