"""COPS-2697: a decommission must not silently delete a surviving
environment's user-content bucket and DNS record.

AE-15284 was a Sev1 of exactly this shape on `pv-toronto-a`. The bucket and the
A record are keyed on `buckets.userContent.suffix`, which every regional
`config.yaml` sets identically for the whole cluster, NOT on `appspace.suffix`.
So a migration clone resolves to the same GCP objects as its original, and
purging the original destroys what the clone still serves from.

The identity strings here are the chart's, verified byte-identical across every
chart version live in production (2603.0.19 / 2603.1.23 / 2603.2.1 / 2601.4.19).
A rename in the chart must break these tests.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import user_content as uc

_UC = "appspace.buckets.userContent."


def _flat(customer="gsk--aec1", suffix="a", regions=("na1",),
          domain="appspacestorage.com", mz_domain="appspacestorage.com",
          mz_enabled=None, enabled=None, core=None, name=None):
    """A merged value chain as `_merged_kcc_flat_for_env` returns one.

    Defaults mirror a real na2-a AEC environment: `name` and
    `managedZone.enabled` are deliberately ABSENT, because no config.yaml in
    the fleet sets them - they come from the chart defaults, and the guard has
    to apply those or it goes blind.
    """
    f = {"appspace.prefix": "pv", "appspace.customerName": customer,
         _UC + "suffix": suffix, _UC + "domain": domain,
         _UC + "managedZone.domain": mz_domain,
         _UC + "managedZone.name": "appspacestorage-com"}
    for r in regions:
        f[_UC + f"regionMapping.{r}.region"] = "us-central1"
    if mz_enabled is not None:
        f[_UC + "managedZone.enabled"] = mz_enabled
    if enabled is not None:
        f[_UC + "enabled"] = enabled
    if core is not None:
        f["appspace.coreTypeName"] = core
    if name is not None:
        f[_UC + "name"] = name
    return f


def test_identity_matches_the_chart_naming():
    """The exact strings the chart renders, defaults applied."""
    i = uc.identity(_flat())
    assert i["proven"] is True
    assert i["buckets"] == {"pv-gsk--aec1-content-na1-a.appspacestorage.com"}
    # trailing dot: the DNSRecordSet `name` carries it
    assert i["fqdns"] == {"pv-gsk--aec1-content-na1-a.appspacestorage.com."}


def test_managed_zone_enabled_defaults_true_or_the_dns_half_goes_blind():
    """No config.yaml sets managedZone.enabled; the chart default is true.

    Defaulting it false would silently switch off DNS detection - which is
    the AE-15284 failure mode, so it gets its own test.
    """
    assert _UC + "managedZone.enabled" not in _flat()
    assert uc.identity(_flat())["fqdns"], "DNS identity must still resolve"


def test_same_customer_different_instance_suffix_collides():
    """The whole point: `-b` and `-c` are different environments, one bucket."""
    torn = uc.identity(_flat())            # pv-gsk--aec1-c being removed
    alive = uc.identity(_flat())           # pv-gsk--aec1-b, untouched
    hits = uc.shared_owners(torn, {"pv-gsk--aec1-b": alive})
    assert list(hits) == ["pv-gsk--aec1-b"]
    assert hits["pv-gsk--aec1-b"]["buckets"] == [
        "pv-gsk--aec1-content-na1-a.appspacestorage.com"]
    assert hits["pv-gsk--aec1-b"]["fqdns"] == [
        "pv-gsk--aec1-content-na1-a.appspacestorage.com."]
    assert hits["pv-gsk--aec1-b"]["unproven"] is False


def test_environment_overriding_its_own_bucket_suffix_does_not_share():
    torn = uc.identity(_flat(suffix="a"))
    alive = uc.identity(_flat(suffix="b"))
    assert uc.shared_owners(torn, {"sibling": alive}) == {}


def test_different_region_key_does_not_share():
    """REGION_KEY is in the name, so a different cluster region is a different
    bucket even for the same customer."""
    torn = uc.identity(_flat(regions=("na1",)))
    alive = uc.identity(_flat(regions=("ca1",)))
    assert uc.shared_owners(torn, {"sibling": alive}) == {}


def test_multi_region_environment_owns_one_object_per_region_key():
    """The chart ranges over regionMapping, so several buckets per env."""
    i = uc.identity(_flat(regions=("na1", "eu1")))
    assert i["buckets"] == {
        "pv-gsk--aec1-content-na1-a.appspacestorage.com",
        "pv-gsk--aec1-content-eu1-a.appspacestorage.com"}
    assert uc.region_keys(_flat(regions=("na1", "eu1"))) == ["eu1", "na1"]


def test_different_customer_never_shares():
    torn = uc.identity(_flat(customer="toronto"))
    alive = uc.identity(_flat(customer="gsk--aec1"))
    assert uc.shared_owners(torn, {"sibling": alive}) == {}


def test_owns_nothing_is_proven_not_unproven():
    """`enabled: false` and a non-user-content coreTypeName render no objects.

    That is a definite "owns nothing" and must NOT read as "might share",
    or every ordinary decommission would warn.
    """
    for f in (_flat(enabled=False), _flat(core="ms")):
        i = uc.identity(f)
        assert i["proven"] is True
        assert i["buckets"] == set() and i["fqdns"] == set()
        assert uc.shared_owners(uc.identity(_flat()), {"s": i}) == {}


def test_unreadable_sibling_is_reported_as_possible_sharer():
    """Fail closed: _merged_kcc_flat_for_env returns None on BB_ERROR, and a
    teardown that might delete live data must not proceed on an unread file."""
    torn = uc.identity(_flat())
    hits = uc.shared_owners(torn, {"unreadable": uc.identity(None)})
    assert hits["unreadable"]["unproven"] is True
    assert hits["unreadable"]["buckets"] == []


def test_partial_chain_is_unproven_not_silently_empty():
    """A chain missing an identity-bearing domain would compute a name that
    cannot match the sibling's real one - a MISSED match, the direction that
    loses data. It must be unproven instead."""
    f = _flat()
    del f[_UC + "domain"]
    assert uc.identity(f)["proven"] is False
    f2 = _flat()
    del f2[_UC + "managedZone.domain"]
    assert uc.identity(f2)["proven"] is False
    f3 = _flat()
    del f3["appspace.customerName"]
    assert uc.identity(f3)["proven"] is False


def test_no_region_mapping_owns_nothing():
    f = _flat(regions=())
    i = uc.identity(f)
    assert i["proven"] is True and i["buckets"] == set()


def test_target_owning_nothing_yields_no_sharers():
    assert uc.shared_owners(uc.identity(_flat(regions=())),
                            {"s": uc.identity(_flat())}) == {}


def test_env_without_any_user_content_block_owns_nothing_not_unproven():
    """Regression. The first implementation tested prefix/customerName BEFORE
    regionMapping, so the ordinary environment - which has no userContent block
    at all - came back `proven: False`. That skipped the cheap early return,
    made every armed-decommission panel run a fleet census, and stamped a
    "check did not complete" warning onto 33 existing tests' panels.

    regionMapping is what the chart ranges over, so it decides existence.
    """
    bare = {"appspace.customerName": "foo", "appspace.decommission": True}
    i = uc.identity(bare)
    assert i["proven"] is True, "no regionMapping means the chart renders none"
    assert i["buckets"] == set() and i["fqdns"] == set()
    # and it must not be reported as a possible sharer of anything
    assert uc.shared_owners(uc.identity(_flat()), {"bare": i}) == {}


# ── the rendering and the verdict ────────────────────────────────────────
# The detector is only half the job: the finding has to reach the reviewer as a
# DO-NOT-MERGE verdict, above the ordinary purge wording. COPS-2668 is the
# precedent — a correct panel under a verdict that contradicted it.

import diff_preview as m                                    # noqa: E402
from comment_render import _DECOM_SHARED_UC_HDR             # noqa: E402


def _stub_owners(monkeypatch, sharers):
    torn = uc.identity(_flat())
    monkeypatch.setattr(m, "_shared_user_content_owners",
                        lambda ident, sha, repo=None: (torn, sharers))


def test_purge_armed_leads_with_the_block_headline_and_names_everything(monkeypatch):
    _stub_owners(monkeypatch, {"pv-gsk--aec1-b": {
        "buckets": ["pv-gsk--aec1-content-na1-a.appspacestorage.com"],
        "fqdns": ["pv-gsk--aec1-content-na1-a.appspacestorage.com."],
        "unproven": False}})
    lines = m._shared_user_content_lines(
        "gcp/aec/private-cloud/na2-a/pv-gsk--aec1-c/customer.yaml", "sha", True)
    assert lines, "an armed purge with a sharer must say something"
    head = lines[0]
    # the sentinel comment_render matches on, first line of the block
    assert _DECOM_SHARED_UC_HDR in head
    # "May be shared" is not actionable: name the sharer and the exact objects
    assert "pv-gsk--aec1-c" in head
    assert "pv-gsk--aec1-b" in head
    assert "pv-gsk--aec1-content-na1-a.appspacestorage.com" in head
    assert "pv-gsk--aec1-content-na1-a.appspacestorage.com." in head
    assert "decommissionPurgeData" in head


def test_purge_not_armed_is_review_not_block(monkeypatch):
    """Abandon, not delete: nothing is destroyed today. Still worth saying, so
    nobody arms the purge later without repointing first."""
    _stub_owners(monkeypatch, {"pv-gsk--aec1-b": {
        "buckets": ["pv-gsk--aec1-content-na1-a.appspacestorage.com"],
        "fqdns": [], "unproven": False}})
    lines = m._shared_user_content_lines(
        "gcp/aec/private-cloud/na2-a/pv-gsk--aec1-c/customer.yaml", "sha", False)
    assert lines
    assert _DECOM_SHARED_UC_HDR not in "\n".join(lines)
    assert "Shared user content" in lines[0]
    assert "abandon" in lines[0]


def test_no_sharer_says_nothing_at_all(monkeypatch):
    """~200 environments decommission without sharing. They must stay quiet."""
    _stub_owners(monkeypatch, {})
    assert m._shared_user_content_lines("a/b/pv-x-a/customer.yaml",
                                        "sha", True) == []


def test_unproven_sibling_is_named_as_a_possible_sharer(monkeypatch):
    _stub_owners(monkeypatch, {"pv-gsk--aec1-b": {
        "buckets": [], "fqdns": [], "unproven": True}})
    body = "\n".join(m._shared_user_content_lines(
        "a/b/pv-gsk--aec1-c/customer.yaml", "sha", True))
    assert "pv-gsk--aec1-b" in body
    assert "unreadable" in body.lower()


def test_a_crash_in_the_check_warns_and_never_hides_the_risk(monkeypatch):
    """P0-6 lesson: an empty result must not read as 'nothing to worry about'."""
    def boom(*a, **kw):
        raise RuntimeError("bitbucket down")
    monkeypatch.setattr(m, "_shared_user_content_owners", boom)
    lines = m._shared_user_content_lines("a/b/pv-x-a/customer.yaml", "sha", True)
    assert lines and "did not complete" in lines[0]
    assert "RuntimeError" in lines[0]
    assert "Verify by hand" in lines[0]


def test_verdict_outranks_the_ordinary_purge_wording():
    """Both headers are present on a shared purge. The verdict must lead with
    the surviving environment's loss, not with the expected self-destruction."""
    from comment_render import _DECOM_PURGE_HDR
    body = m.format_comment(
        "a" * 40, {}, base_sha="b" * 40,
        decommission_lines=["\U0001f6a8 " + _DECOM_SHARED_UC_HDR + " shared.",
                            "", "\U0001f6a8 " + _DECOM_PURGE_HDR + " purged."])
    assert "SHARED" in body or "shared" in body
    assert "surviving environment" in body
    # the plain purge wording must not be the headline when both are present
    i_shared = body.find("surviving environment")
    i_purge = body.find("buckets/datasets are destroyed")
    assert i_shared >= 0
    assert i_purge == -1 or i_shared < i_purge
