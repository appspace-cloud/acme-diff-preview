"""Produce a realistic PR comment from the REAL renderer, for the README GIF.

Not a mockup: this drives the production `format_comment` exactly as the
goldens do, so the GIF shows what the service actually posts.

The scenario is the everyday one — a monthly customer version bump rolled out
across a fleet of environments — because that is what a reviewer sees most
weeks, and because it is where the tool earns its keep: 24 environments taking
the identical transition fold into a single line, and the two that are NOT
like the others are pulled out and named.
"""
import os
import sys

os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, os.path.join(REPO, "tests"))

import diff_preview as m
from fleet_names import FLEET, NEEDLE_ENV, UNTOUCHED

PR_SHA = "9f4c1a70b2e83d5169ac4f0e7b83c21d5e9a4471"
BASE_SHA = "3b81c02f7d64ae95c1f0b7d2843e6a19cf70b558"
ART = "https://argocd.appspace.com/diff/acme-config-prod/4312/9f4c1a70"

m._ts = lambda: "2026-08-15 09:14 UTC"
m.generate_ai_summary = lambda app_results: None
m._repo_for_sha = lambda sha: "acme-config-prod"

OLD, NEW = "2603.4.1", "2603.5.0"

# The monthly bump itself: one image tag, the same everywhere.
def bump_hunk(svc):
    return ("--- \n+++ \n@@ -18,7 +18,7 @@\n"
            "     spec:\n"
            "       containers:\n"
            f"         - name: {svc}\n"
            f"-          image: appspace-{svc}:{OLD}\n"
            f"+          image: appspace-{svc}:{NEW}\n"
            "           ports:\n"
            "             - containerPort: 8080\n")

# The needle: one environment where the bump drags a replica count to zero.
NEEDLE = ("--- \n+++ \n@@ -7,7 +7,7 @@\n"
          "   name: signschannel\n"
          " spec:\n"
          "-  replicas: 4\n"
          "+  replicas: 0\n"
          "   selector:\n")


def _res(text, sections, n, outcome, **kw):
    return m.DiffResult(text, sections, n, bool(text), None, outcome,
                        kw.get("reason"), kw.get("version_change"),
                        kw.get("deleted"), kw.get("zeroed"), kw.get("fp"))


results = {}

# 24 environments across three cells taking the identical transition.
for env in FLEET:
    h = bump_hunk("platformservice")
    results[f"{env}-ms"] = _res(
        h, [("/apps/Deployment platformservice", h)], 1, m.OUT_DIFF,
        version_change=(OLD, NEW))

# The needle: same bump, but this one also zeroes a workload.
needle_env = NEEDLE_ENV
results[f"{needle_env}-ss"] = _res(
    NEEDLE, [("/apps/Deployment signschannel", NEEDLE)], 1, m.OUT_DIFF,
    zeroed=["/apps/Deployment signschannel"])

# A couple of environments genuinely untouched by the bump.
for env in UNTOUCHED:
    results[f"{env}-ms"] = _res("", [], 0, m.OUT_NO_DIFF)

body = m.format_comment(PR_SHA, results, base_sha=BASE_SHA, artifact_url=ART)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comment.md")
with open(out, "w") as f:
    f.write(body)
print(f"{len(body)} bytes, {len(body.splitlines())} lines, "
      f"{len(results)} apps -> {out}")
for line in body.splitlines()[:12]:
    print("   ", line[:104])
