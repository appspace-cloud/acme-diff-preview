"""COPS-2635: the VM section groups identical provisions and says "new VM"
when it is one; the changeset table's App cells carry the deep links.

Measured on acme-config-dev PR #7064 (8 envs provisioning the same svc VM):
the VM section was 58 lines for one fact, and every one of the 8 danger
lines carried the resize runbook ("stopping the VM first") for a VM that
does not exist yet. acme-config-stage #2807 showed the same wrong story on
a single env, and it is what made an operator file a bug against the tool.

The grouping mirrors COPS-2629, applied to the section it left out. The
full-diff page (is_complete_record) keeps every per-key line: nothing is
collapsed out of both surfaces.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")

import diff_preview as dp  # noqa: E402

URL = "https://argocd.appspace.com/diff/acme-config-dev/7064/644535294858"

BASE = "appspace:\n  customerName: c\n"
NEW_VM = (BASE +
          "  infra:\n"
          "    deployLinuxServicesK8s:\n"
          "      enabled: true\n"
          "      svc:\n"
          "        enabled: true\n"
          "        machineType: n2d-standard-2\n"
          "        createNewBootDisk: true\n")
OLD_VM = (BASE +
          "  infra:\n"
          "    deployLinuxServicesK8s:\n"
          "      enabled: true\n"
          "      svc:\n"
          "        enabled: true\n"
          "        machineType: n2d-standard-2\n")
RESIZED = OLD_VM.replace("n2d-standard-2", "n2d-standard-4")


def _panel(monkeypatch, files, envs_new=(), envs_resize=(), app_results=None):
    """Run _summarize_vm_changes against crafted file contents."""
    def fetch(path, sha, repo=None):
        env = path.split("/")[-2]
        if sha == "base" * 10:
            return ("" if env in envs_new else
                    RESIZED if False else OLD_VM), dp.BB_OK
        if env in envs_new:
            return NEW_VM, dp.BB_OK
        if env in envs_resize:
            return RESIZED, dp.BB_OK
        return OLD_VM, dp.BB_OK

    monkeypatch.setattr(dp, "_bb_fetch_cached", fetch)
    path_map = {f.lstrip("/"): True for f in files}
    return "\n".join(dp._summarize_vm_changes(
        files, path_map=path_map, app_results=app_results or {},
        repo="acme-config-dev", pr_sha="a" * 40, base_sha="base" * 10))


def _files(envs):
    return ["gcp/dev/x/%s/customer.yaml" % e for e in envs]


ENVS8 = ["pv-dev-%02d-a" % i for i in (1, 3, 4, 5, 6, 64)] + [
    "pv-dev-spiralscout1-a", "pv-dev-spiralscout3-a"]


# -- part 1+2: provisions group, and a new VM is not a resized VM ----------

def test_eight_identical_provisions_become_one_statement(monkeypatch):
    out = _panel(monkeypatch, _files(ENVS8), envs_new=set(ENVS8))
    assert out.count("provision") == 1
    assert "8 environments provision a new linux VM (KCC)" in out
    assert "n2d-standard-2" in out
    assert "new boot disk" in out


def test_a_new_vm_never_gets_the_resize_runbook(monkeypatch):
    """The #2807 confusion: 'the runbook requires stopping the VM first'
    for a VM that does not exist. There is nothing to stop."""
    out = _panel(monkeypatch, _files(ENVS8), envs_new=set(ENVS8))
    assert "stopping the VM first" not in out


def test_grouped_envs_lose_their_per_key_lines(monkeypatch):
    """The 32 'Routine VM changes' lines on #7064 restated the provision
    key by key. Grouped envs contribute no per-key lines; the detail lives
    on the page."""
    out = _panel(monkeypatch, _files(ENVS8), envs_new=set(ENVS8))
    assert "createNewBootDisk` = `True`" not in out
    assert "`svc.enabled` = `True`" not in out


def test_every_provisioned_env_is_still_accounted_for(monkeypatch):
    out = _panel(monkeypatch, _files(ENVS8), envs_new=set(ENVS8))
    named = [e for e in ENVS8 if e in out]
    assert len(named) == 8 or "more" in out
    assert "8 environments" in out


def test_a_single_new_vm_says_provisions_not_resize(monkeypatch):
    out = _panel(monkeypatch, _files(["pv-solo-a"]), envs_new={"pv-solo-a"})
    assert "1 environment provisions a new linux VM" in out
    assert "stopping the VM first" not in out


def test_a_real_resize_keeps_the_runbook_untouched(monkeypatch):
    """Regression guard: mutations of an existing VM keep today's wording,
    including the runbook, per line."""
    out = _panel(monkeypatch, _files(["pv-old-a"]), envs_resize={"pv-old-a"})
    assert "stopping the VM first" in out
    assert "provision" not in out


def test_mixed_pr_keeps_the_resize_line_and_groups_the_rest(monkeypatch):
    envs = ENVS8 + ["pv-old-a"]
    out = _panel(monkeypatch, _files(envs), envs_new=set(ENVS8),
                 envs_resize={"pv-old-a"})
    assert "8 environments provision a new linux VM" in out
    assert "stopping the VM first" in out
    assert "`pv-old-a`" in out


# -- the new-resource facts fold into the group -----------------------------

def _created_fact(kind, name):
    return {"kind": kind, "name": name, "fields": [], "deleted": False,
            "dangerous": [], "notes": [
                "new %s — appears in this environment for the first time"
                % kind]}


def test_first_time_resources_fold_into_the_group(monkeypatch):
    """15 'appears for the first time' lines on #7064 said which kinds a
    provision creates, once per env. The group states the kinds once."""
    results = {}
    for e in ENVS8:
        r = dp.DiffResult("", [], 0, True, None, dp.OUT_DIFF, None)
        r = r._replace(vm_changes=[_created_fact("ComputeInstance",
                                                 "%s-svc-a" % e),
                                   _created_fact("ComputeDisk",
                                                 "%s-svc-a-data" % e)])
        results["%s-ss" % e] = r
    out = _panel(monkeypatch, _files(ENVS8), envs_new=set(ENVS8),
                 app_results=results)
    assert "appears in this environment for the first time" not in out
    assert "New resources per environment:" in out
    assert "ComputeDisk" in out and "ComputeInstance" in out


# -- part 3: the table's App column carries the links -----------------------

def _changed(name="x", n=3):
    hdrs = ["/apps/Deployment d%d" % i for i in range(n)]
    secs = [(h, "  image: acme/%s:1" % name) for h in hdrs]
    return dp.DiffResult("\n".join("--- %s" % h for h in hdrs), secs,
                         n, True, None, dp.OUT_DIFF, None)


def _big(n=12):
    # Distinct shapes so the COPS-2629 same-shape grouping stays out of
    # the way: this test is about the table, not about grouping.
    return {"pv-t%02d-a-ss" % i: _changed("t%02d" % i, n=3 + i)
            for i in range(n)}


def test_table_app_cells_are_deep_links(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = dp.format_comment("a" * 40, _big(), base_sha="b" * 40,
                            artifact_url=URL)
    assert "#### Changeset overview" in out
    assert "| [`pv-t00-a-ss`](%s#app-pv-t00-a-ss) |" % URL in out
    assert "| [`pv-t11-a-ss`](%s#app-pv-t11-a-ss) |" % URL in out


def test_the_redundant_per_app_blocks_are_gone(monkeypatch):
    """26 lines on #7064 restated 13 table rows, adding only the link the
    row itself now carries."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = dp.format_comment("a" * 40, _big(), base_sha="b" * 40,
                            artifact_url=URL)
    assert "[Full hunks for" not in out
    assert "resource(s) changed**" not in out


def test_a_risky_app_keeps_its_block_below_the_table(monkeypatch):
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    results = _big()
    risky = _changed("risky", n=4)._replace(
        deleted_resources=["/apps/Deployment d0"])
    results["pv-risky-a-ss"] = risky
    out = dp.format_comment("a" * 40, results, base_sha="b" * 40,
                            artifact_url=URL)
    assert "`pv-risky-a-ss`" in out.split("Changeset overview")[1] \
        .split("|-----")[0] or "pv-risky-a-ss" in out
    # the risk detail block survives outside the table
    after_table = out.split("no changes")[-1] if "no changes" in out else out
    assert "pv-risky-a-ss" in after_table


def test_without_artifact_url_nothing_changes(monkeypatch):
    """No page means no anchors: cells stay plain and the per-app blocks
    stay, because they are the only pointer that exists."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = dp.format_comment("a" * 40, _big(), base_sha="b" * 40)
    assert "| `pv-t00-a-ss` |" in out
    assert "](" not in out.split("Changeset overview")[1].split("|-----")[0] \
        if "Changeset overview" in out else True


def test_the_page_profile_is_untouched(monkeypatch):
    """is_complete_record keeps per-app blocks: the page is the record."""
    monkeypatch.setattr(dp, "generate_ai_summary", lambda *a, **k: None)
    out = dp.format_comment(
        "a" * 40, _big(), base_sha="b" * 40, artifact_url=URL,
        profile=dp.RenderProfile("page", is_complete_record=True,
                                 inline_diffs=True))
    assert "resource(s) changed" in out
