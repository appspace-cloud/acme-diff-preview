#!/usr/bin/env python3
"""Periodic hard-refresh for the ArgoCD projects that track mutable tags.

Runs from the acme-diff-preview-hard-refresh CronJob as a safety net; the
JFrog webhook is the primary trigger. Triggers a hard refresh on every app
in the configured projects so ArgoCD re-pulls the OCI Helm chart even when
the tag has not changed (mutable -dev tags are overwritten on each CI build).

Hard refresh bypasses the Redis manifest cache and forces the
repo-server to re-download the .tgz from the OCI registry.

Which projects (COPS-2543): any project whose apps track MUTABLE chart tags.
That is dev, qa AND stage - 35 of the 38 stage apps point at `-dev` tags, so
leaving stage out (as this script did until v2.13.0) meant stage had no cron
safety net behind the webhook at all. Prod is deliberately excluded: it pins
immutable `-rev1` tags, so a refresh can never find anything new, and walking
its 761 apps daily would hammer the hub for nothing.

Deliberately NOT reusing ARGOCD_PROJECTS (the webhook path's list): that one
includes appspace-prod on purpose, because the webhook only refreshes the
apps actually tracking the chart that was just published - a targeted set.
This CronJob refreshes EVERY app in the listed projects, so it needs its own,
narrower list.
"""
import concurrent.futures
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

# COPS-2702: this env name is SHARED with the diff-preview service, which can
# now be pointed at the in-cluster Service (argocd-server.argocd.svc:80,
# plaintext). Keep both in step: if ARGOCD_SERVER ever names a cluster-local
# address here, ARGOCD_PLAINTEXT must be set too, or this script would attempt
# TLS against a plaintext port. The CronJob template deliberately sets neither.
SERVER    = os.environ.get("ARGOCD_SERVER", "argocd.appspace.com")
PLAINTEXT = os.environ.get("ARGOCD_PLAINTEXT", "").strip().lower() in (
    "1", "true", "yes", "on")
if PLAINTEXT and "." in SERVER.split(":")[0] and ".svc" not in SERVER:
    sys.exit("FATAL: ARGOCD_PLAINTEXT set but ARGOCD_SERVER is not in-cluster; "
             "refusing to send credentials in cleartext.")
ARGOCD    = os.environ.get("ARGOCD_BIN", "/usr/local/bin/argocd")
PROJECTS  = [p.strip() for p in os.environ.get(
    "HARD_REFRESH_PROJECTS", "appspace-dev,appspace-qa,appspace-stage").split(",")
    if p.strip()]
def _env_int(name, default):
    """Read a positive int from env, falling back on anything unusable."""
    try:
        value = int(os.environ.get(name, "").strip())
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# Concurrency (v2.13.0, COPS-2543): lowered 8 -> 3.
#
# `argocd app get --hard-refresh` is SYNCHRONOUS: the server holds the request
# open while the repo-server re-pulls the chart and re-renders, which measures
# ~19s per app even when the hub is idle. That is already close to the 30s
# timeout on the GCP load balancer backend in front of argocd.appspace.com, so
# there is very little headroom to spend on queueing. Under the old 8-way fan
# out, sustained for the whole run, 20 of 120 apps got cut off by the LB at
# exactly 30.1s. Every one of them was a `pv-*` app, i.e. hosted on a SPOKE and
# reached through the argocd-agent principal - the component with the known
# ~9.5 events/s per spoke ceiling from COPS-2540. Refreshing 8 at a time drives
# the principal past that and the slowest ones fall off the LB.
#
# This is a daily safety net (the JFrog webhook is the primary trigger), so
# wall-clock time is worth nothing here and being gentle is worth a lot:
# 3 workers over ~158 apps is ~17min instead of ~5min, and stays inside the
# per-request budget instead of racing it.
WORKERS   = _env_int("HARD_REFRESH_WORKERS", 3)
# Stagger between submissions so a pool slot freeing up does not immediately
# fire the next request in the same instant as its two siblings.
PACE      = float(os.environ.get("HARD_REFRESH_PACE", "0.5") or 0.5)
# 60s per app — TimeoutExpired is caught inside hard_refresh() so a
# single slow app never crashes the entire ThreadPoolExecutor pool.
# Note this is the CLIENT budget; the LB cuts at 30s well before this, which
# is why a cut shows up as a plain failure rather than a TimeoutExpired.
TIMEOUT   = _env_int("HARD_REFRESH_TIMEOUT", 60)
# One retry per app (v2.13.0): a 30s LB cut is transient by nature, and losing
# an app's refresh for a whole day because it queued behind a slow spoke is
# exactly what this job exists to prevent.
ATTEMPTS  = _env_int("HARD_REFRESH_ATTEMPTS", 2)

# --insecure removed: argocd.appspace.com has a valid CA-signed certificate.
BASE_FLAGS = [
    "--server", SERVER,
    "--grpc-web",
] + (["--plaintext"] if PLAINTEXT else [])


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
    scheme   = "http" if PLAINTEXT else "https"
    url      = f"{scheme}://{SERVER}/api/v1/session"
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
    """Hard-refresh one app, retrying once. Returns (app, success, elapsed_secs).

    elapsed is the LAST attempt's duration, not the sum, so main() can still
    tell a client-side TimeoutExpired (>= TIMEOUT) apart from a load-balancer
    cut at ~30s just by looking at it.
    """
    last_err = ""
    elapsed = 0.0
    for attempt in range(ATTEMPTS):
        # Stagger: keeps the workers from firing in the same instant, and
        # spaces a retry out from the failure that caused it instead of
        # slamming a hub that is evidently already busy.
        if PACE:
            time.sleep(PACE)
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                [ARGOCD, "app", "get", app, "--hard-refresh"] + BASE_FLAGS,
                capture_output=True, text=True, timeout=TIMEOUT,
            )
            elapsed = round(time.monotonic() - t0, 1)
            if r.returncode == 0:
                return app, True, elapsed
            # 200 chars, not 80: the old truncation cut the argocd CLI's JSON
            # error in half ("...POST https://argocd.app") and hid whether it
            # was a timeout, a 502 or a permission problem.
            last_err = " ".join(r.stderr[:200].split())
        except subprocess.TimeoutExpired:
            elapsed = round(time.monotonic() - t0, 1)
            print(f"  WARN: {app}: timed out after {elapsed}s", flush=True)
            return app, False, elapsed
    print(f"  WARN: {app}: failed after {ATTEMPTS} attempts ({elapsed}s) {last_err}",
          flush=True)
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
    print(f"Hard-refreshing {len(apps)} apps in {', '.join(PROJECTS)} ...",
          flush=True)
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