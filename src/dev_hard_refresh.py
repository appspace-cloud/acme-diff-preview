#!/usr/bin/env python3
"""Periodic hard-refresh for dev/qa ArgoCD apps.

Runs every 2 hours from the argocd-dev-hard-refresh CronJob.
Triggers a hard refresh on all apps in appspace-dev and appspace-qa
projects so ArgoCD re-pulls the OCI Helm chart even when the tag has
not changed (mutable -dev tags overwritten on each CI build).

Hard refresh bypasses the Redis manifest cache and forces the
repo-server to re-download the .tgz from the OCI registry.
This is targeted only at dev/qa environments - staging and
production apps are not touched.
"""
import concurrent.futures
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

SERVER    = os.environ.get("ARGOCD_SERVER", "argocd.appspace.com")
ARGOCD    = os.environ.get("ARGOCD_BIN", "/usr/local/bin/argocd")
PROJECTS  = ["appspace-dev", "appspace-qa"]
WORKERS   = 8
# 60s per app — TimeoutExpired is caught inside hard_refresh() so a
# single slow app never crashes the entire ThreadPoolExecutor pool.
TIMEOUT   = 60

# --insecure removed: argocd.appspace.com has a valid CA-signed certificate.
BASE_FLAGS = [
    "--server", SERVER,
    "--grpc-web",
]


def _fetch_argocd_token() -> str:
    """Obtain a short-lived JWT from ArgoCD REST session API.

    Uses ARGOCD_USER / ARGOCD_PASS from env (injected by ExternalSecret).
    The token is exported as ARGOCD_AUTH_TOKEN so the argocd CLI picks it up
    without needing `argocd login`, keeping ARGOCD_PASS off the process list.

    DUPLICATED LOGIC: diff_preview.py has an identical implementation
    (_argocd_fetch_token). This script is deliberately standalone — the
    CronJob container runs it as a single file with no imports from the
    service — so extraction into a shared module was rejected on purpose.
    If the ArgoCD session endpoint, auth payload, or TLS handling changes,
    UPDATE BOTH copies.
    """
    user     = os.environ.get("ARGOCD_USER", "diff-preview")
    password = os.environ["ARGOCD_PASS"]
    url      = f"https://{SERVER}/api/v1/session"
    data     = json.dumps({"username": user, "password": password}).encode()
    req      = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # CA-verified TLS (default context) — no CERT_NONE.
    ssl_ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:
        return json.loads(resp.read())["token"]

def list_apps():
    args = [ARGOCD, "app", "list", "-o", "name"] + BASE_FLAGS
    for p in PROJECTS:
        args += ["--project", p]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"ERROR: argocd app list failed: {r.stderr[:200]}", flush=True)
        sys.exit(1)
    apps = []
    for line in r.stdout.strip().splitlines():
        name = line.strip().split("/", 1)[-1]
        if name:
            apps.append(name)
    return apps

def hard_refresh(app):
    """Hard-refresh one app. Returns (app, success, elapsed_secs)."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [ARGOCD, "app", "get", app, "--hard-refresh"] + BASE_FLAGS,
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        elapsed = round(time.monotonic() - t0, 1)
        ok = r.returncode == 0
        if not ok:
            print(f"  WARN: {app}: failed ({elapsed}s) {r.stderr[:80]}", flush=True)
        return app, ok, elapsed
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - t0, 1)
        print(f"  WARN: {app}: timed out after {elapsed}s", flush=True)
        return app, False, elapsed

def main():
    # Authenticate once via REST: sets ARGOCD_AUTH_TOKEN in the process env
    # so all argocd CLI calls below pick it up without `argocd login`.
    # ARGOCD_PASS never touches the process argument list this way.
    print("Authenticating to ArgoCD via REST session API ...", flush=True)
    try:
        token = _fetch_argocd_token()
        os.environ["ARGOCD_AUTH_TOKEN"] = token
        print("ArgoCD authentication OK.", flush=True)
    except Exception as exc:
        print(f"ERROR: ArgoCD authentication failed: {exc}", flush=True)
        sys.exit(1)

    apps = list_apps()
    t_start = time.monotonic()
    print(f"Hard-refreshing {len(apps)} dev/qa apps ...", flush=True)
    ok = 0
    timeouts = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for app, success, elapsed in pool.map(hard_refresh, apps):
            if success:
                ok += 1
                print(f"  OK: {app} [{elapsed}s]", flush=True)
            else:
                if elapsed >= TIMEOUT:
                    timeouts += 1
    total = round(time.monotonic() - t_start, 1)
    print(
        f"Done: {ok}/{len(apps)} refreshed, {timeouts} timed out "
        f"[total {total}s].",
        flush=True
    )

if __name__ == "__main__":
    main()