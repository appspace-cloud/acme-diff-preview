"""COPS-2694: fleet health gauges for the mass-degradation alert.

The hub's application-controller runs 0 replicas (argocd-agent managed mode),
so argocd_app_info does not exist anywhere and nothing exports per-Application
health. This module closes that gap from data the service already holds: the
`argocd app list -o json` payload that discover_path_app_map() fetches every
PATH_MAP_TTL seconds carries status.health/status.sync and spec.destination
for every Application on the hub - including the ones destined to the Azure
spokes, which no Google-side metric could otherwise see.

Metric contract (same rule as _PROM_REGISTRY in diff_preview: a metric is a
contract with whatever alerts on it; these names are referenced verbatim by
the Cloud Monitoring policies in acme-infrastructure
deployments/appspace-com/gcp/appspace-devops/_global/monitoring/argocd-alerts):

  acme_diff_preview_argocd_app_health{dest_cluster,health_status}  gauge
      Applications per destination cluster and health status, as reported on
      the hub. Only observed (cluster, status) pairs are emitted - a missing
      series means zero, which the alert-side sum/ratio arithmetic handles.
  acme_diff_preview_argocd_app_sync{dest_cluster,sync_status}      gauge
      Same, for sync status.
  acme_diff_preview_fleet_snapshot_age_seconds                     gauge
      Seconds since the counts were last rebuilt from a successful app list.
      The alert side treats "absent OR > 900" as "the paging pipeline is
      blind" - so this series doubles as the staleness heartbeat.

Leader-only on purpose: discovery runs in the poll loop, which only the
leader executes, so the standby's snapshot would only ever go stale. The
/metrics handler passes is_leader and the standby emits NOTHING from this
module. The alert queries still collapse duplicate emitters defensively with
an inner max by () in case both replicas ever lead during a lease flap.

Failure semantics mirror the P0-6 lesson: collect() is only reached after a
successful `argocd app list` parse, a malformed individual app is skipped
(never raises into discovery), and a failing list simply lets the age grow
until the staleness alert fires. No fallback value is ever invented.
"""
import threading
import time

# Statuses are emitted verbatim from ArgoCD (Healthy, Progressing, Degraded,
# Suspended, Missing, Unknown / Synced, OutOfSync, Unknown). An absent field
# is Unknown: that is what ArgoCD itself displays for a status it cannot
# compute, and inventing a healthier default would blind the alert.
_UNKNOWN = "Unknown"

_lock = threading.Lock()
_health_counts: dict = {}     # (dest_cluster, health_status) -> int
_sync_counts: dict = {}       # (dest_cluster, sync_status) -> int
_collected_monotonic: float = 0.0   # 0.0 = never collected in this process


def _dest_cluster(app) -> str:
    """Destination identity for grouping - name first, server URL fallback.

    Every fleet app sets spec.destination.name (the argocd-agent agent name).
    Apps targeting the hub itself use the in-cluster server URL instead; they
    are grouped as "in-cluster" rather than dropped so the fleet totals stay
    a bijection with what `argocd app list` returned.
    """
    dest = (app.get("spec") or {}).get("destination") or {}
    name = dest.get("name")
    if name:
        return str(name)
    server = dest.get("server") or ""
    if server == "https://kubernetes.default.svc":
        return "in-cluster"
    return str(server) if server else _UNKNOWN


def collect(apps) -> int:
    """Rebuild the counts from a parsed `argocd app list -o json` payload.

    Returns the number of apps counted. Individual malformed entries are
    skipped; only the outer container being wrong (not iterable) raises,
    and the caller wraps this call so discovery can never be broken by it.
    """
    health: dict = {}
    sync: dict = {}
    counted = 0
    for app in apps:
        try:
            dest = _dest_cluster(app)
            status = app.get("status") or {}
            h = (status.get("health") or {}).get("status") or _UNKNOWN
            s = (status.get("sync") or {}).get("status") or _UNKNOWN
        except AttributeError:
            continue   # not a dict-shaped app entry; skip, never raise
        health[(dest, str(h))] = health.get((dest, str(h)), 0) + 1
        sync[(dest, str(s))] = sync.get((dest, str(s)), 0) + 1
        counted += 1
    global _health_counts, _sync_counts, _collected_monotonic
    with _lock:
        _health_counts = health
        _sync_counts = sync
        _collected_monotonic = time.monotonic()
    return counted


def _escape(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def render_prometheus(is_leader, now=time.monotonic) -> str:
    """Exposition block appended to /metrics by the health handler.

    Empty string when not leading or before the first collect: an absent
    series is the unambiguous signal the alert-side absent() guard keys on,
    and a standby replica emitting zeros would double-count under sum().
    """
    with _lock:
        collected = _collected_monotonic
        health = dict(_health_counts)
        sync = dict(_sync_counts)
    if not is_leader or collected == 0.0:
        return ""
    out = [
        "# HELP acme_diff_preview_argocd_app_health Applications per "
        "destination cluster and health status, from the hub Application CRs.",
        "# TYPE acme_diff_preview_argocd_app_health gauge",
    ]
    for (dest, status), n in sorted(health.items()):
        out.append(
            'acme_diff_preview_argocd_app_health{dest_cluster="%s",'
            'health_status="%s"} %d' % (_escape(dest), _escape(status), n))
    out += [
        "# HELP acme_diff_preview_argocd_app_sync Applications per "
        "destination cluster and sync status, from the hub Application CRs.",
        "# TYPE acme_diff_preview_argocd_app_sync gauge",
    ]
    for (dest, status), n in sorted(sync.items()):
        out.append(
            'acme_diff_preview_argocd_app_sync{dest_cluster="%s",'
            'sync_status="%s"} %d' % (_escape(dest), _escape(status), n))
    out += [
        "# HELP acme_diff_preview_fleet_snapshot_age_seconds Seconds since "
        "the fleet counts were rebuilt from a successful argocd app list.",
        "# TYPE acme_diff_preview_fleet_snapshot_age_seconds gauge",
        "acme_diff_preview_fleet_snapshot_age_seconds %g"
        % max(0.0, now() - collected),
    ]
    return "\n".join(out) + "\n"
