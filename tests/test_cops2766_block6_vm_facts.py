"""COPS-2766 block 6, items 1 and 3: VM facts from the render, as warnings.

Item 3. KCC runs a machineType change as stop, resize, start (the chart sets
`allow-stopping-for-update`), so every resize is flagged, parked or not. A
move to TERMINATED gets a note (the stop loop, COPR-31983 / COPS-2760), and
TERMINATED to RUNNING gets a note too. A ComputeDisk `location` change is
immutable like the zone.

Item 1. Some changes render fine and KCC rejects them on sync: a bootDisk
change on an existing VM (#3931, #4239), and a BigQueryDataset or
StorageBucket location or project change (#4517). The hunk cannot show where
a line sits, so both renders are parsed. Everything here is a warning: the
token stays [clean] and the build stays green.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m  # noqa: E402
import logsink  # noqa: E402
import manifest  # noqa: E402
import vm_analysis as vma  # noqa: E402

CI_HDR = "/compute.cnrm.cloud.google.com/ComputeInstance pv-qa88-a/pv-qa88-svc-a"
CD_HDR = "/compute.cnrm.cloud.google.com/ComputeDisk pv-qa88-a/pv-qa88-svc-a-data"
CI_KEY = ("compute.cnrm.cloud.google.com/ComputeInstance", "pv-qa88-a",
          "pv-qa88-svc-a")
BQ_KEY = ("bigquery.cnrm.cloud.google.com/BigQueryDataset", "",
          "pv-acme-analytics-a")
SB_KEY = ("storage.cnrm.cloud.google.com/StorageBucket", "",
          "pv-acme-content-backup")
HOSTING = "bootDisk.initializeParams.labels.hosting-id"


def _fact(header, body):
    facts = vma._detect_vm_changes([(header, body)])
    assert len(facts) == 1
    return facts[0]


# ── (a) machineType and desiredStatus in the hunk ─────────────────────────

RESIZE_PARKED = (
    "     zone: europe-west1-d\n"
    "     desiredStatus: TERMINATED\n"
    "-    machineType: n2d-standard-4\n"
    "+    machineType: n2d-standard-8\n"
)
RESIZE_AND_PARK = (
    "     zone: europe-west1-d\n"
    "-    machineType: n2d-standard-4\n"
    "+    machineType: n2d-standard-8\n"
    "-    desiredStatus: \"RUNNING\"\n"
    "+    desiredStatus: \"TERMINATED\"\n"
)
PARK_ONLY = (
    "     zone: europe-west1-d\n"
    "-    desiredStatus: RUNNING\n"
    "+    desiredStatus: TERMINATED\n"
)
START_ONLY = (
    "     zone: europe-west1-d\n"
    "-    desiredStatus: TERMINATED\n"
    "+    desiredStatus: RUNNING\n"
)


def test_a_resize_on_a_parked_vm_is_dangerous():
    f = _fact(CI_HDR, RESIZE_PARKED)
    assert f["dangerous"] == [vma._VM_RESIZE_REASON]
    assert f["notes"] == []


def test_a_resize_with_a_park_is_dangerous_and_names_the_stop_loop():
    f = _fact(CI_HDR, RESIZE_AND_PARK)
    assert f["dangerous"] == [vma._VM_RESIZE_REASON]
    assert f["notes"] == [vma._VM_PARK_NOTE]


def test_a_park_alone_is_a_note():
    f = _fact(CI_HDR, PARK_ONLY)
    assert f["dangerous"] == [] and f["notes"] == [vma._VM_PARK_NOTE]


def test_a_start_is_a_note_and_not_a_danger():
    f = _fact(CI_HDR, START_ONLY)
    assert f["dangerous"] == [] and f["notes"] == [vma._VM_START_NOTE]


def test_the_texts_say_what_kcc_does():
    assert vma._VM_RESIZE_REASON == (
        "machineType changes: KCC stops, resizes and starts the VM. Merge in "
        "a window. Do not park with TERMINATED")
    assert "COPR-31983" in vma._VM_PARK_NOTE and "COPS-2760" in vma._VM_PARK_NOTE
    assert vma._VM_START_NOTE.endswith("KCC starts the VM on sync")


# ── (b) ComputeDisk location ──────────────────────────────────────────────

def test_a_disk_location_change_is_dangerous():
    f = _fact(CD_HDR, "   size: 100\n-  location: us-central1-a\n"
                      "+  location: us-central1-b\n")
    assert ("location", "us-central1-a", "us-central1-b") in f["fields"]
    assert f["dangerous"] == [
        "disk location is immutable: KCC rejects the change and the sync fails"]


def test_a_disk_location_that_stays_is_not_dangerous():
    f = _fact(CD_HDR, "   location: us-central1-a\n-  size: 100\n+  size: 200\n")
    assert f["dangerous"] == []


# ── (c) _render_immutable_facts on parsed renders ─────────────────────────

def _ci(meta_id="hst-00000000", boot_id="hst-00000000", q='"', new_boot=True):
    """A rendered ComputeInstance, the way the kcc-linux-services chart does it."""
    if new_boot:
        boot = (
            "  bootDisk:\n"
            "    autoDelete: true\n"
            "    initializeParams:\n"
            "      sourceImageRef:\n"
            f"        external: {q}projects/debian-cloud/global/images/debian-12{q}\n"
            "      size: 64\n"
            f"      type: {q}pd-ssd{q}\n"
            "      labels:\n"
            f"        hosting-id: {q}{boot_id}{q}\n")
    else:
        boot = (
            "  bootDisk:\n"
            "    autoDelete: true\n"
            "    sourceDiskRef:\n"
            f"      external: {q}https://www.googleapis.com/compute/v1/projects/"
            f"p/zones/us-central1-a/disks/pv-qa88-svc-a{q}\n")
    return (
        "apiVersion: compute.cnrm.cloud.google.com/v1beta1\n"
        "kind: ComputeInstance\n"
        "metadata:\n"
        "  name: pv-qa88-svc-a\n"
        "  namespace: pv-qa88-a\n"
        "  labels:\n"
        f"    hosting-id: {q}{meta_id}{q}\n"
        "spec:\n"
        f"  resourceID: {q}pv-qa88-svc-a{q}\n"
        f"  zone: {q}us-central1-a{q}\n"
        f"  machineType: {q}n2d-standard-4{q}\n"
        f"  desiredStatus: {q}RUNNING{q}\n" + boot)


def _bq(location="us-east1", project="appspace-cloud-private-bq", ref=True):
    """A rendered BigQueryDataset. The chart gives it no namespace."""
    return (
        "apiVersion: bigquery.cnrm.cloud.google.com/v1beta1\n"
        "kind: BigQueryDataset\n"
        "metadata:\n"
        "  name: pv-acme-analytics-a\n"
        "  annotations:\n"
        "    cnrm.cloud.google.com/deletion-policy: abandon\n"
        "spec:\n"
        f"  location: {location}\n"
        "  resourceID: pv_acme_analytics_a\n"
        + (f"  projectRef:\n    external: {project}\n" if ref else ""))


def _bucket(project="appspace-backup", location="US-CENTRAL1"):
    return (
        "apiVersion: storage.cnrm.cloud.google.com/v1beta1\n"
        "kind: StorageBucket\n"
        "metadata:\n"
        "  name: pv-acme-content-backup\n"
        "  annotations:\n"
        f"    cnrm.cloud.google.com/project-id: \"{project}\"\n"
        "spec:\n"
        "  storageClass: STANDARD\n"
        f"  location: \"{location}\"\n")


def _facts(key, old, new):
    return vma._render_immutable_facts({key: old}, {key: new})


def test_4239_hosting_id_in_the_boot_disk_is_one_dangerous_fact():
    facts = _facts(CI_KEY, _ci(), _ci(meta_id="hst-00000478",
                                      boot_id="hst-00000478"))
    assert len(facts) == 1
    f = facts[0]
    assert f["header"] == CI_HDR and f["kind"] == "ComputeInstance"
    assert f["name"] == "pv-qa88-svc-a"
    assert f["fields"] == [(HOSTING, "hst-00000000", "hst-00000478")]
    assert f["dangerous"] == [vma._BOOT_DISK_REASON]
    assert f["notes"] == []
    assert not (f["created"] or f["deleted"] or f["orphaned"])
    assert vma._BOOT_DISK_REASON.startswith(
        "bootDisk is fixed when the VM is created: KCC rejects any change")


def test_a_metadata_label_change_alone_gives_nothing():
    assert _facts(CI_KEY, _ci(), _ci(meta_id="hst-00000478")) == []


def test_a_re_quote_alone_gives_nothing():
    assert _facts(CI_KEY, _ci(q='"'), _ci(q="")) == []


def test_a_create_new_boot_disk_flip_is_dangerous():
    facts = _facts(CI_KEY, _ci(new_boot=False), _ci(new_boot=True))
    assert len(facts) == 1
    keys = [k for k, _o, _n in facts[0]["fields"]]
    assert "bootDisk.sourceDiskRef.external" in keys
    assert HOSTING in keys
    assert "bootDisk.autoDelete" not in keys, "only the leaves that differ"
    assert ("bootDisk.initializeParams.size", "", "64") in facts[0]["fields"]
    assert facts[0]["dangerous"] == [vma._BOOT_DISK_REASON]


def test_a_boolean_leaf_prints_as_yaml_and_not_as_absent():
    old = _ci().replace("autoDelete: true", "autoDelete: false")
    facts = _facts(CI_KEY, old, _ci())
    assert facts[0]["fields"] == [("bootDisk.autoDelete", "false", "true")]


def test_4517_a_bigquery_location_move_is_dangerous():
    facts = _facts(BQ_KEY, _bq(location="us-east1"), _bq(location="us-central1"))
    assert len(facts) == 1
    f = facts[0]
    assert f["kind"] == "BigQueryDataset"
    assert f["fields"] == [("location", "us-east1", "us-central1")]
    assert f["dangerous"] == [vma._DATA_LOCATION_REASON]


def test_the_data_reasons_say_first_that_they_are_about_data():
    """They render under the VM panel header, so the first words place them."""
    assert vma._DATA_LOCATION_REASON.startswith("data location is immutable: ")
    assert vma._DATA_PROJECT_REASON.startswith(
        "the project of the data changes: ")


def test_a_location_case_change_is_not_a_move():
    assert _facts(SB_KEY, _bucket(location="US-CENTRAL1"),
                  _bucket(location="us-central1")) == []


def test_a_project_ref_change_is_dangerous():
    facts = _facts(BQ_KEY, _bq(project="appspace-cloud-private-bq"),
                   _bq(project="appspace-cloud-stage-bq"))
    assert facts[0]["fields"] == [
        ("project", "appspace-cloud-private-bq", "appspace-cloud-stage-bq")]
    assert facts[0]["dangerous"] == [vma._DATA_PROJECT_REASON]


def test_a_project_ref_added_is_a_note_only():
    facts = _facts(BQ_KEY, _bq(ref=False), _bq(ref=True))
    assert facts[0]["dangerous"] == []
    assert facts[0]["notes"] == [vma._DATA_PROJECT_NOTE]


def test_a_project_ref_by_name_counts_as_the_project():
    old = _bq(ref=False) + "  projectRef:\n    name: proj-a\n"
    new = _bq(ref=False) + "  projectRef:\n    name: proj-b\n"
    assert _facts(BQ_KEY, old, new)[0]["dangerous"] == [vma._DATA_PROJECT_REASON]


def test_a_bucket_project_id_annotation_change_is_dangerous():
    facts = _facts(SB_KEY, _bucket(project="appspace-backup"),
                   _bucket(project="appspace-backup-2"))
    assert facts[0]["kind"] == "StorageBucket"
    assert facts[0]["dangerous"] == [vma._DATA_PROJECT_REASON]


def test_a_new_resource_gives_nothing():
    assert vma._render_immutable_facts({}, {CI_KEY: _ci()}) == []


def test_bad_yaml_is_skipped():
    assert _facts(CI_KEY, _ci(), "kind: ComputeInstance\nspec: [unclosed\n") == []


def test_a_doc_that_is_not_a_mapping_is_skipped():
    assert _facts(CI_KEY, _ci(), "- just\n- a list\n") == []


def test_other_kinds_are_ignored():
    key = ("apps/Deployment", "pv-qa88-a", "web")
    assert _facts(key, "kind: Deployment\nspec:\n  location: a\n",
                  "kind: Deployment\nspec:\n  location: b\n") == []


def test_odd_shapes_never_raise():
    """spec null, bootDisk a string, annotations a list: no crash."""
    ci_null = ("kind: ComputeInstance\nmetadata:\n  name: pv-qa88-svc-a\n"
               "spec: null\n")
    ci_str = ("kind: ComputeInstance\nmetadata:\n  name: pv-qa88-svc-a\n"
              "spec:\n  bootDisk: x\n")
    assert _facts(CI_KEY, ci_null, ci_str) == []
    bq_list = ("kind: BigQueryDataset\nmetadata:\n  annotations: []\n"
               "spec:\n  location: us-east1\n  projectRef: x\n")
    assert _facts(BQ_KEY, bq_list, bq_list + "  resourceID: y\n") == []


def test_an_unexpected_error_is_a_missed_warning_and_not_a_crash(monkeypatch):
    warned = []
    monkeypatch.setattr(logsink, "log",
                        lambda msg, *a, **k: warned.append((msg, a)))
    bad_key = ("only", "two")  # not a (type_key, ns, name) key
    assert vma._render_immutable_facts({bad_key: "a"}, {bad_key: "b"}) == []
    assert warned and warned[0][1] == ("WARNING",)


def test_a_namespace_less_bigquery_fact_matches_the_section_header():
    main_res = manifest._parse_manifest_resources(_bq(location="us-east1"))
    pr_res = manifest._parse_manifest_resources(_bq(location="us-central1"))
    assert list(pr_res) == [BQ_KEY]
    sections = m.parse_diff_sections(manifest._diff_resources(main_res, pr_res))
    facts = vma._render_immutable_facts(main_res, pr_res)
    assert [f["header"] for f in facts] == [h for h, _ in sections]
    assert facts[0]["header"] == ("/bigquery.cnrm.cloud.google.com/"
                                  "BigQueryDataset pv-acme-analytics-a")


def test_a_namespace_less_bucket_fact_matches_the_section_and_is_exempt():
    """The critic case: the backup bucket renders no namespace either. Its
    fact header must be the section header, so the fold never takes it."""
    main_res = manifest._parse_manifest_resources(_bucket("appspace-backup"))
    pr_res = manifest._parse_manifest_resources(_bucket("appspace-backup-2"))
    assert list(pr_res) == [SB_KEY]
    sections = m.parse_diff_sections(manifest._diff_resources(main_res, pr_res))
    facts = vma._render_immutable_facts(main_res, pr_res)
    assert [f["header"] for f in facts] == [h for h, _ in sections]
    assert facts[0]["header"] == ("/storage.cnrm.cloud.google.com/"
                                  "StorageBucket pv-acme-content-backup")
    packed = m._package_sections(sections, render_facts=facts)
    assert [f["header"] for f in packed[6]] == [h for h, _ in sections], (
        "one fact for the bucket, not a hunk fact plus a render fact")


def test_a_project_ref_format_change_is_not_a_move():
    """A chart release that only moves `projects/<id>` to `<id>`, or
    `external` to `name`, names the same project: no fleet-wide nag."""
    old = _bq(project="projects/appspace-cloud-private-bq")
    new = _bq(ref=False) + "  projectRef:\n    name: appspace-cloud-private-bq\n"
    assert _facts(BQ_KEY, old, new) == []
    assert _facts(BQ_KEY, _bq(), _bq(project="projects/appspace-cloud-private-bq")) == []


def test_a_project_ref_move_prints_the_bare_ids():
    facts = _facts(BQ_KEY, _bq(project="projects/proj-a"),
                   _bq(project="projects/proj-b"))
    assert facts[0]["fields"] == [("project", "proj-a", "proj-b")]
    assert facts[0]["dangerous"] == [vma._DATA_PROJECT_REASON]


def test_a_size_that_only_changes_its_quoting_is_not_a_boot_disk_change():
    """`size: 64` and `size: "64"` parse to int and str. The same text is the
    same value, like the hunk reader says."""
    old = _ci(q="").replace("size: 64", 'size: "64"')
    assert _facts(CI_KEY, old, _ci(q="")) == []


def test_a_boot_disk_size_or_image_change_is_a_boot_disk_danger():
    """bootDiskSizeGb and bootImage render into initializeParams, and KCC
    rejects any change there (the template comment says so)."""
    grown = _ci().replace("size: 64", "size: 128")
    facts = _facts(CI_KEY, _ci(), grown)
    assert facts[0]["fields"] == [("bootDisk.initializeParams.size", "64", "128")]
    assert facts[0]["dangerous"] == [vma._BOOT_DISK_REASON]
    image = _ci().replace("debian-12", "debian-13")
    facts = _facts(CI_KEY, _ci(), image)
    assert [k for k, _o, _n in facts[0]["fields"]] == [
        "bootDisk.initializeParams.sourceImageRef.external"]


# ── (d) _merge_vm_facts ───────────────────────────────────────────────────

def _base(header=CI_HDR, fields=None, dangerous=None, notes=None):
    return {"header": header, "kind": "ComputeInstance", "name": "x",
            "fields": list(fields or []), "created": False, "deleted": False,
            "orphaned": False, "dangerous": list(dangerous or []),
            "notes": list(notes or [])}


def test_merge_folds_into_the_same_header_without_duplicates():
    hunk = [_base(fields=[("machineType", "a", "b")], notes=["n1"])]
    extra = [_base(fields=[("machineType", "a", "b"), (HOSTING, "x", "y")],
                   dangerous=["d1"], notes=["n1", "n2"])]
    out = vma._merge_vm_facts(hunk, extra)
    assert len(out) == 1
    assert out[0]["fields"] == [("machineType", "a", "b"), (HOSTING, "x", "y")]
    assert out[0]["dangerous"] == ["d1"]
    assert out[0]["notes"] == ["n1", "n2"]


def test_merge_appends_a_new_header():
    other = _base(header="/bigquery.cnrm.cloud.google.com/BigQueryDataset b")
    out = vma._merge_vm_facts([_base()], [other])
    assert [f["header"] for f in out] == [CI_HDR, other["header"]]


def test_merge_with_nothing_extra_keeps_the_facts():
    facts = [_base()]
    assert vma._merge_vm_facts(facts, None) == facts


def test_merge_names_a_boot_disk_size_once():
    """The hunk reads the boot disk `size` and `type` with no path. The
    render fact names the same change with its path, so only that one
    stays. A hunk value that differs is kept, and the hunk danger stays."""
    shrink = "disk size DECREASES"
    hunk = [_base(fields=[("size", "128", "64"), ("type", "pd-ssd", "pd-ssd2"),
                          ("machineType", "a", "b")], dangerous=[shrink])]
    extra = [_base(fields=[("bootDisk.initializeParams.size", "128", "64"),
                           ("bootDisk.initializeParams.type", "pd-ssd",
                            "pd-balanced")],
                   dangerous=[vma._BOOT_DISK_REASON])]
    out = vma._merge_vm_facts(hunk, extra)
    assert out[0]["fields"] == [
        ("type", "pd-ssd", "pd-ssd2"), ("machineType", "a", "b"),
        ("bootDisk.initializeParams.size", "128", "64"),
        ("bootDisk.initializeParams.type", "pd-ssd", "pd-balanced")]
    assert out[0]["dangerous"] == [shrink, vma._BOOT_DISK_REASON]


def test_the_4239_shape_says_the_field_once_and_does_not_call_it_untracked():
    """#4239: the metadata label and the boot disk label move together. The
    hunk sees two `hosting-id` lines with no path, the render names the boot
    disk one with its path."""
    old = _ci()
    new = _ci(meta_id="hst-00000478", boot_id="hst-00000478")
    hunk = "".join(
        "-" + a + "\n+" + b + "\n" if a != b else " " + a + "\n"
        for a, b in zip(old.splitlines(), new.splitlines()))
    facts = vma._detect_vm_changes([(CI_HDR, hunk)])
    assert facts[0]["untracked"] == ["hosting-id"]
    assert facts[0]["notes"] == [vma._untracked_note(["hosting-id"])]
    out = vma._merge_vm_facts(facts, _facts(CI_KEY, old, new))
    assert out[0]["notes"] == [] and out[0]["untracked"] == []
    assert out[0]["fields"] == [(HOSTING, "hst-00000000", "hst-00000478")]


def test_merge_keeps_the_untracked_keys_the_render_does_not_name():
    keys = ["hosting-id"] + ["k%d" % i for i in range(9)]
    hunk = [_base(notes=["n1", vma._untracked_note(keys)])]
    hunk[0]["untracked"] = keys
    out = vma._merge_vm_facts(hunk, [_base(fields=[(HOSTING, "a", "b")])])
    assert out[0]["untracked"] == keys[1:]
    assert out[0]["notes"] == ["n1", vma._untracked_note(keys[1:])]
    assert out[0]["notes"][1].endswith("`k7` and 1 more")


def test_merge_leaves_the_note_when_the_render_names_another_key():
    hunk = [_base(notes=[vma._untracked_note(["foo"])])]
    hunk[0]["untracked"] = ["foo"]
    out = vma._merge_vm_facts(hunk, [_base(fields=[(HOSTING, "a", "b")])])
    assert out[0]["notes"] == [vma._untracked_note(["foo"])]


def test_the_hub_re_exports_the_new_names():
    for name in ("_VM_RESIZE_REASON", "_VM_PARK_NOTE", "_VM_START_NOTE",
                 "_IMMUTABLE_RENDER_KINDS", "_render_immutable_facts",
                 "_merge_vm_facts"):
        assert getattr(m, name) is getattr(vma, name), name


# ── (e) end to end: render, retry wrapper, panel and comment ──────────────

APP = "pv-qa88-a-ss"
PR_SHA, MAIN_SHA = "prsha0000001", "mainsha00001"


def _world(monkeypatch, pr_doc, main_doc):
    monkeypatch.setitem(m._app_chart_map, APP, "supporting-services")
    monkeypatch.setitem(m._app_chart_revision_map, APP, "2604.0.1")
    monkeypatch.setitem(m._app_chart_registry_map, APP, "registry.example.com")
    monkeypatch.setitem(m._app_value_files_map, APP,
                        ["$config/gcp/qa/pv-qa88-a/customer.yaml"])
    monkeypatch.setitem(m._app_namespace_map, APP, "pv-qa88-a")
    monkeypatch.setattr(m, "_ensure_chart", lambda reg, chart, ver: "/fake/chart")
    monkeypatch.setattr(m, "_fetch_value_files", lambda vfs, sha: {
        vf: f"side: {'pr' if sha == PR_SHA else 'main'}\n" for vf in vfs})
    monkeypatch.setattr(m, "_main_render_content_key", lambda *a: "k-vmf")
    monkeypatch.setattr(m, "_main_render_cache_get", lambda k: (None, None, "miss"))
    monkeypatch.setattr(m, "_main_render_cache_put", lambda *a: None)

    def fake_template(chart, release, namespace, vals):
        pr = "side: pr" in "".join(vals.values())
        return (pr_doc if pr else main_doc), None
    monkeypatch.setattr(m, "_helm_template", fake_template)


def test_run_one_diff_carries_the_render_facts(monkeypatch):
    _world(monkeypatch, _ci(boot_id="hst-00000478"), _ci())
    step = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert step[1] is None and len(step) == 8
    assert [f["fields"] for f in step[7]] == [
        [(HOSTING, "hst-00000000", "hst-00000478")]]


def test_step_7_is_the_render_facts_and_nothing_else(monkeypatch):
    """Other blocks may grow the same success tuple. If a rebase moves an
    element into index 7, this fails instead of the panel reading it."""
    _world(monkeypatch, _ci(), _ci())
    step = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert step[7] == []
    _world(monkeypatch, _ci(boot_id="hst-00000478"), _ci())
    step = m._run_one_diff(APP, PR_SHA, MAIN_SHA)
    assert isinstance(step[7], list) and step[7]
    for f in step[7]:
        assert isinstance(f, dict)
        assert {"header", "kind", "fields", "dangerous", "notes"} <= set(f)


def test_a_boot_disk_growth_shows_one_field_in_the_panel(monkeypatch):
    """The hunk sees `size` and the render sees the path: one field, and the
    growth is a boot disk danger now."""
    _world(monkeypatch, _ci().replace("size: 64", "size: 128"), _ci())
    r = m.argocd_diff(APP, PR_SHA, MAIN_SHA)
    assert r.vm_changes[0]["fields"] == [
        ("bootDisk.initializeParams.size", "64", "128")]
    assert r.vm_changes[0]["dangerous"] == [vma._BOOT_DISK_REASON]
    text = "\n".join(m._summarize_vm_changes([], PR_SHA, MAIN_SHA, {}, {APP: r}))
    assert "`size` `64`" not in text
    assert "`bootDisk.initializeParams.size` `64` → `128`" in text


def test_the_boot_disk_warning_reaches_the_panel_and_stays_clean(monkeypatch):
    _world(monkeypatch, _ci(boot_id="hst-00000478"), _ci())
    r = m.argocd_diff(APP, PR_SHA, MAIN_SHA)
    assert r.outcome == m.OUT_DIFF
    assert len(r.vm_changes) == 1, "the hunk fact and the render fact are one"
    f = r.vm_changes[0]
    assert f["header"] == CI_HDR
    assert (HOSTING, "hst-00000000", "hst-00000478") in f["fields"]
    assert f["dangerous"] == [vma._BOOT_DISK_REASON]
    lines = m._summarize_vm_changes([], PR_SHA, MAIN_SHA, {}, {APP: r})
    assert lines[0] == m._VM_PANEL_DANGER_HDR
    text = "\n".join(lines)
    assert "`bootDisk.initializeParams.labels.hosting-id`" in text
    assert vma._BOOT_DISK_REASON in text
    assert "not individually tracked" not in text, "the render names it"
    body = m.format_comment(PR_SHA, {APP: r}, base_sha=MAIN_SHA,
                            vm_change_lines=lines)
    assert m._extract_status_token(body) == "clean"


def test_a_bigquery_move_is_never_folded_as_version_noise():
    bump = [(f"/apps/Deployment svc-{i}",
             "--- \n+++ \n@@ -1,4 +1,4 @@\n   labels:\n"
             "-    helm.sh/chart: supporting-services-2603.0.1\n"
             "+    helm.sh/chart: supporting-services-2604.0.1\n")
            for i in range(3)]
    bq_hdr = "/bigquery.cnrm.cloud.google.com/BigQueryDataset pv-acme-analytics-a"
    secs = bump + [(bq_hdr, bump[0][1])]
    vc = ("2603.0.1", "2604.0.1")
    plain = m._package_sections(secs, version_change=vc)
    assert bq_hdr in plain[7]["headers"], "the control: it folds without a fact"
    fact = _base(header=bq_hdr, dangerous=[vma._DATA_LOCATION_REASON])
    packed = m._package_sections(secs, version_change=vc, render_facts=[fact])
    assert bq_hdr not in packed[7]["headers"]
    assert [f["header"] for f in packed[6]] == [bq_hdr]


def test_the_panel_shows_the_park_note_next_to_the_resize(monkeypatch):
    secs = [(CI_HDR, RESIZE_AND_PARK)]
    r = m.DiffResult(RESIZE_AND_PARK, secs, 1, True, None, m.OUT_DIFF,
                     "changes", vm_changes=m._detect_vm_changes(secs))
    text = "\n".join(m._summarize_vm_changes([], PR_SHA, MAIN_SHA, {}, {APP: r}))
    line = next(ln for ln in text.splitlines() if "ComputeInstance" in ln)
    assert vma._VM_RESIZE_REASON in line and vma._VM_PARK_NOTE in line
    assert ".;" not in line, "the reasons join with no stray period"


def test_the_panel_shows_the_start_note_on_a_routine_line():
    secs = [(CI_HDR, START_ONLY)]
    r = m.DiffResult(START_ONLY, secs, 1, True, None, m.OUT_DIFF,
                     "changes", vm_changes=m._detect_vm_changes(secs))
    lines = m._summarize_vm_changes([], PR_SHA, MAIN_SHA, {}, {APP: r})
    assert lines[0] == m._VM_PANEL_ROUTINE_HDR
    line = next(ln for ln in lines if "ComputeInstance" in ln)
    assert "`desiredStatus` `TERMINATED` → `RUNNING`" in line
    assert vma._VM_START_NOTE in line
