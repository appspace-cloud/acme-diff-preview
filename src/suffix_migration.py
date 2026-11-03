"""Two cells of one customer: what a suffix migration must not do.

A customer moves to a new cell by adding a folder with another suffix
(`pv-<customer>-a` to `pv-<customer>-b`) and cutting traffic over later. Both
cells keep the customerName, so the load-balancer chart gives both the same
public A record, `<customerSubdomain or customerName>.<domain>`, pointing at
each cell's own address. The cell that turns the record on takes the name with
Config Connector at its next sync. Nothing waits for the new load balancer,
the data, or the decision to cut over.

Mirrored from `helm-charts/load-balancer/templates/{gcp,azure}/private-cloud/
common/compute-dnsrecordset.yaml` (2603 release lines, every 2604 dev line):

  zone, domain     from `<cloudShortName>-<deploymentType>`, never from
                   customer.yaml. Azure prod takes the nginx `subdomainType`.
  on               `loadBalancers.dns.managedZone.enabled` (chart default
                   false; acme-config-dev turns it on for every GCP cell).
  skipped          a GCP cell with an SFW address, and an Azure dev, qa or
                   stage cell whose `subdomainType` is another suffix.
  deletion-policy  delete when `decommission` is true, abandon otherwise.

An Azure record also needs `loadBalancers.gatewayApi.az.publicIP`, which a cell
only has after its first sync. The flag alone counts here: it is the intent,
and the record follows as soon as the address is set. A cell on a chart line
without its deployment type in the map renders nothing yet, and counts too.

The cutover these findings allow, one PR each:

  1. the new cell, with the record off;
  2. the old cell turns the record off (abandon keeps it in Cloud DNS);
  3. once Argo has pruned the old DNSRecordSet, the new cell turns it on.

Steps 2 and 3 in one PR are refused. The two apps sync in any order, so the
old controller can write its address back after the new one, and the prune
then leaves it there until the new cell's next reconcile.
"""
import vm_analysis

_GCP = {
    "gcp-pv-dev": ("dev-appspace-com", "dev.appspace.com"),
    "gcp-pv-qa": ("dev-appspace-com", "dev.appspace.com"),
    "gcp-pv-stage": ("stage-appspace-com", "stage.appspace.com"),
    "gcp-pv-prod": ("appspace-com", "cloud.appspace.com"),
    "gcp-pv-aec": ("appspace-com", "cloud.appspace.com"),
}
_AZ = {
    "az-pv-dev": ("dev-appspace-com", "dev.appspace.com"),
    "az-pv-qa": ("dev-appspace-com", "dev.appspace.com"),
    "az-pv-stage": ("stage-appspace-com", "stage.appspace.com"),
}
FLAG = "loadBalancers.dns.managedZone.enabled"
# The merged keys the findings read, apart from the KCC VM keys.
KEYS = tuple("appspace." + k for k in (
    "environmentType", "cloudShortName", "deploymentType", "customerName",
    "customerSubdomain", "infra.az.loadBalancers.nginxFrontend.subdomainType", FLAG,
    "cloudServices.cloudSfwAddress", "cloudServices.cloudApicSfwAddress", "decommission",
    "infra.gcp.project", "prefix", "suffix"))


def _get(flat, key):
    return (flat or {}).get("appspace." + key)


def _true(v) -> bool:
    return str(v).strip().lower() == "true"


def _served(flat) -> str:
    return str(_get(flat, "infra.az.loadBalancers.nginxFrontend.subdomainType") or "").strip()


def _parts(flat):
    """(kind, zone, domain, host) of the env's LB record, or None."""
    if _get(flat, "environmentType") != "private-cloud":
        return None
    kind = f"{_get(flat, 'cloudShortName')}-{_get(flat, 'deploymentType')}"
    host = _get(flat, "customerSubdomain") or _get(flat, "customerName")
    if kind in _GCP:
        zone, domain = _GCP[kind]
    elif kind in _AZ:
        zone, domain = _AZ[kind]
    elif kind == "az-pv-prod":
        zone, domain = "appspace-com", _served(flat)
    else:
        return None
    return (kind, zone, domain, str(host)) if host and domain else None


def public_name(flat):
    """(zone, fqdn) the env answers on, whether it publishes the record or not."""
    p = _parts(flat)
    return (p[1], f"{p[3]}.{p[2]}") if p else None


def record_owner(flat):
    """public_name when the chart writes the record for this env, else None."""
    p = _parts(flat)
    if not p or not _true(_get(flat, FLAG)):
        return None
    kind, zone, domain, host = p
    if kind.startswith("gcp-") and (_get(flat, "cloudServices.cloudSfwAddress")
                                    or _get(flat, "cloudServices.cloudApicSfwAddress")):
        return None
    if kind in _AZ and _served(flat) != domain:
        return None
    return zone, f"{host}.{domain}"


def kcc_vms(flat) -> set:
    """(GCP project, VM name) of every KCC Linux VM the env renders."""
    if _get(flat, "cloudShortName") != "gcp":
        return set()
    project = str(_get(flat, "infra.gcp.project") or "")
    return {(project, n) for role in vm_analysis._kcc_rendered_roles(flat)
            for n in vm_analysis._kcc_instance_names(flat, role) if n}


def _names(envs) -> str:
    return ", ".join(f"`{e}`" for e in sorted(envs))


def _gate(kind, env, why, others, text):
    return {"kind": kind, "env": env, "why": why, "others": sorted(others), "text": text}


def findings(base: dict, head: dict) -> list:
    """The gates of the PR that turns `base` into `head`. Both are {env: merged
    flat values} of the live private clouds; an env only in head is new, one
    only in base is removed. Only envs the PR changes are judged, and the
    others only as their siblings, so a state already on main does not block
    an unrelated PR."""
    changed = {e for e in set(base) | set(head) if base.get(e) != head.get(e)}
    new = set(head) - set(base)
    out = []

    def owners(side, name):
        return {e for e, f in side.items() if record_owner(f) == name}

    def siblings(env, name):
        """The other envs that answer on `name` after the PR."""
        return {e for e, f in head.items() if e != env and public_name(f) == name}

    for name in sorted({record_owner(head[e]) for e in changed & set(head)} - {None}):
        zone, fqdn = name
        before, after = owners(base, name), owners(head, name)
        for env in sorted((after - before) & changed):
            rivals = (before | after) - {env}
            if rivals:
                where = "on main" if rivals & before else "in this PR"
                out.append(_gate("dns_two_owners", env, fqdn, rivals, (
                    f"`{env}` turns on the record `{fqdn}` ({zone}) while {_names(rivals)} "
                    f"also writes it {where}. Two Config Connector resources would overwrite "
                    "each other at every reconcile. Turn it off on the old cell in its own PR, "
                    "wait until Argo has pruned that DNSRecordSet (abandon keeps the record), "
                    "then turn it on here.")))
                continue
            sibs = siblings(env, name)
            if env in new and sibs:
                out.append(_gate("dns_new_sibling", env, fqdn, sibs, (
                    f"New `{env}` turns on the record `{fqdn}`, and {_names(sibs)} answers on that "
                    f"name today. At its first sync Config Connector points `{fqdn}` at "
                    f"`{env}`, before its load balancer serves, before the data is in, and "
                    "before anyone decides to cut over. Create it with "
                    f"`appspace.{FLAG}: false` and turn it on in the cutover PR.")))

    for env in sorted(head):
        name = record_owner(head[env])
        if not name or not _true(_get(head[env], "decommission")):
            continue
        sibs = {s for s in siblings(env, name) if not _true(_get(head[s], "decommission"))}
        if sibs and ({env} | sibs) & changed:
            out.append(_gate("dns_decommission_shared", env, name[1], sibs, (
                f"`{env}` is armed for deletion and still writes `{name[1]}`, now with "
                f"`deletion-policy: delete`. {_names(sibs)} answers on that name, and removing "
                f"the folder deletes the record. Turn the record off on `{env}` first "
                "(abandon keeps it), and finish the cutover.")))

    pairs = set()
    for env in sorted(changed & set(head)):
        mine = kcc_vms(head[env])
        for other in sorted(head):
            both = mine & kcc_vms(head[other]) if other != env else set()
            if both and frozenset((env, other)) not in pairs:
                pairs.add(frozenset((env, other)))
                vms = ", ".join(f"`{n}`" for _, n in sorted(both))
                out.append(_gate("vm_shared", env, ", ".join(n for _, n in sorted(both)),
                                 {other}, (
                    f"`{env}` and `{other}` both render the KCC VM {vms}. Both manage one "
                    "machine, and decommissioning either deletes it. Give the new cell its own "
                    "VM names, or release the VM from the old cell first (the role "
                    "`enabled: false`, abandon), then adopt it here.")))
    return out
