"""Kubernetes Lease-based leader election (stdlib only).

One replica at a time must run the PR poll loop; the others stay hot,
serving HTTP, ready to take over. This module implements the standard
client-go election algorithm over a coordination.k8s.io/v1 Lease object,
talking to the API server directly with urllib and the pod's in-cluster
ServiceAccount, so the service keeps its zero-dependency footprint.

Design points, mirrored from client-go's tools/leaderelection:

- Optimistic concurrency: every PUT echoes the resourceVersion from the
  previous GET. The API server rejects a stale write with 409 Conflict,
  so exactly one replica wins each update. That 409 IS the election.
- Local observation for expiry: a standby never trusts the timestamps in
  the lease against its own clock (clock skew). It remembers WHEN IT
  FIRST SAW the current (holder, renewTime) pair, and only considers the
  lease expired after lease_duration passes on its own clock without
  that pair changing.
- Self-demotion: is_leader() is only true while the last successful
  renewal is younger than renew_deadline on the local clock. If the API
  server becomes unreachable, leadership decays by itself, with no
  network call needed to answer is_leader().
- Fast handoff: release() blanks holderIdentity on shutdown, so the
  standby takes over on its next tick instead of waiting out a full
  lease_duration.
- Not fencing: like client-go, this cannot guarantee a paused leader
  never overlaps a new one for a moment. The caller's work must be
  idempotent (ours is: one deterministic comment per PR, upserted).

Single-process modes: with enabled=False, or when no in-cluster
ServiceAccount exists (local runs, tests), is_leader() is always True
and nothing touches the network. A single replica in the cluster simply
always wins, so shipping this at replicas=1 changes nothing.
"""
from __future__ import annotations

import datetime
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request

DEFAULT_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
DEFAULT_API_HOST = "https://kubernetes.default.svc"
_HTTP_TIMEOUT = 5


def _microtime(ts: float) -> str:
    """RFC3339 MicroTime: exactly 6 fractional digits and a Z suffix.

    The API server's MicroTime parser wants this exact shape;
    datetime.isoformat() is wrong for it (variable precision, +00:00).
    """
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class LeaderElector:
    """Elect a leader on one Lease; hand out a cheap local is_leader()."""

    def __init__(self, lease_name: str, identity: str, *,
                 namespace: str | None = None,
                 sa_dir: str = DEFAULT_SA_DIR,
                 api_host: str = DEFAULT_API_HOST,
                 lease_duration: int = 15,
                 renew_deadline: int = 10,
                 retry_period: float = 2.0,
                 enabled: bool = True,
                 verify_tls: bool = True,
                 on_event=None,
                 clock=time.time):
        self._name = lease_name
        self._id = identity
        self._ns = namespace
        self._sa_dir = sa_dir
        self._api = api_host.rstrip("/")
        self._duration = lease_duration
        self._deadline = renew_deadline
        self._retry = retry_period
        self._enabled = enabled
        self._verify_tls = verify_tls
        self._on_event = on_event
        self._clock = clock

        self._lock = threading.Lock()
        self._leading = False
        self._last_renew = 0.0
        # Local observation of someone else's lease: ((holder, renewTime),
        # first_seen_at_local_clock). See the module docstring.
        self._observed = None
        self._bootstrapped: bool | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- public API ----------------------------------------------------------

    def is_leader(self) -> bool:
        """Cheap local answer, no network. Self-demotes past the deadline."""
        if not self._enabled or not self._bootstrap():
            return True  # single-process mode: this instance owns the loop
        with self._lock:
            return (self._leading
                    and (self._clock() - self._last_renew) < self._deadline)

    def start(self) -> None:
        """Run the election loop in a daemon thread (no-op off-cluster)."""
        if not self._enabled or not self._bootstrap():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="leader-election")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._retry + _HTTP_TIMEOUT)

    def release(self) -> None:
        """Blank holderIdentity so the standby takes over fast. Best-effort.

        Also ends this elector's participation for good: once shutdown
        begins, a stray election tick must never re-acquire the released
        lease (stealing it back from the standby while this pod drains),
        and a shutting-down standby must not acquire leadership either.
        """
        if not self._enabled or not self._bootstrap():
            return
        self._stop_event.set()  # no further ticks, leader or standby
        was_leader = self.is_leader()
        with self._lock:
            self._leading = False  # local demotion first, unconditionally
        if not was_leader:
            return
        try:
            lease, rv = self._get_lease()
            if lease is not None and (
                    lease["spec"].get("holderIdentity") == self._id):
                lease["spec"]["holderIdentity"] = ""
                lease["metadata"]["resourceVersion"] = rv
                self._put_lease(lease)
                self._event("released leadership for fast handoff")
        except Exception as e:
            self._event(f"lease release failed (non-fatal): {e}")

    def tick(self) -> None:
        """One election round. Never raises: a broken tick must never take
        down the poll loop thread, and leadership decays on its own anyway.
        A no-op once shutdown began (see release)."""
        if self._stop_event.is_set():
            return
        try:
            self._tick()
        except Exception as e:
            self._event(f"leader election tick failed (non-fatal): {e}")

    # -- election round ------------------------------------------------------

    def _tick(self) -> None:
        now = self._clock()
        lease, rv = self._get_lease()

        if lease is None:  # no lease yet: try to be first
            self._set_leading(self._create_lease(now), now)
            return

        spec = lease.get("spec", {})
        holder = spec.get("holderIdentity") or ""

        if holder == self._id:  # renew our own
            spec["renewTime"] = _microtime(now)
            lease["metadata"]["resourceVersion"] = rv
            self._set_leading(self._put_lease(lease), now)
            return

        # Someone else's lease (or a released one): take over only when it
        # is released, or expired by our LOCAL observation window.
        key = (holder, spec.get("renewTime", ""))
        if self._observed is None or self._observed[0] != key:
            self._observed = (key, now)
        expired = (holder == ""
                   or (now - self._observed[1]) > self._duration)
        if not expired:
            self._set_leading(False, now)
            return
        spec["holderIdentity"] = self._id
        spec["leaseDurationSeconds"] = self._duration
        spec["acquireTime"] = _microtime(now)
        spec["renewTime"] = _microtime(now)
        spec["leaseTransitions"] = int(spec.get("leaseTransitions") or 0) + 1
        lease["metadata"]["resourceVersion"] = rv
        self._set_leading(self._put_lease(lease), now)

    def _set_leading(self, leading: bool, now: float) -> None:
        with self._lock:
            was = self._leading
            self._leading = leading
            if leading:
                self._last_renew = now
                self._observed = None
        if leading and not was:
            self._event(f"acquired leadership of lease {self._name}")
        elif was and not leading:
            self._event(f"lost leadership of lease {self._name}")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.tick()
            self._stop_event.wait(self._retry)

    # -- Kubernetes API over urllib -------------------------------------------

    def _lease_url(self, create: bool = False) -> str:
        base = (f"{self._api}/apis/coordination.k8s.io/v1/"
                f"namespaces/{self._namespace()}/leases")
        return base if create else f"{base}/{self._name}"

    def _request(self, method: str, url: str, body: dict | None = None):
        # The token is re-read from disk on EVERY request: GKE bound tokens
        # rotate (~1h TTL), a token cached at process start eventually 401s.
        with open(os.path.join(self._sa_dir, "token")) as f:
            token = f.read().strip()
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        if self._verify_tls:  # pragma: no cover - real TLS needs a cluster
            ctx = ssl.create_default_context(
                cafile=os.path.join(self._sa_dir, "ca.crt"))
            return urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT,
                                          context=ctx)
        return urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)

    def _get_lease(self):
        """Return (lease dict, resourceVersion) or (None, None) on 404."""
        try:
            with self._request("GET", self._lease_url()) as r:
                lease = json.load(r)
            return lease, lease["metadata"]["resourceVersion"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            raise

    def _create_lease(self, now: float) -> bool:
        body = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": self._name, "namespace": self._namespace()},
            "spec": {
                "holderIdentity": self._id,
                "leaseDurationSeconds": self._duration,
                "acquireTime": _microtime(now),
                "renewTime": _microtime(now),
                "leaseTransitions": 0,
            },
        }
        try:
            with self._request("POST", self._lease_url(create=True), body):
                pass
            return True
        except urllib.error.HTTPError as e:
            if e.code == 409:  # another replica created it first: they win
                return False
            raise

    def _put_lease(self, lease: dict) -> bool:
        try:
            with self._request("PUT", self._lease_url(), lease):
                pass
            return True
        except urllib.error.HTTPError as e:
            if e.code == 409:  # lost the CAS race: someone else updated first
                return False
            raise

    # -- environment ----------------------------------------------------------

    def _bootstrap(self) -> bool:
        """True when an in-cluster ServiceAccount is mounted. Cached."""
        if self._bootstrapped is None:
            ok = os.path.isfile(os.path.join(self._sa_dir, "token"))
            if not ok:
                self._event(
                    "no in-cluster ServiceAccount found: leader election "
                    "off, acting as a single instance")
            self._bootstrapped = ok
        return self._bootstrapped

    def _namespace(self) -> str:
        if self._ns is None:
            try:
                with open(os.path.join(self._sa_dir, "namespace")) as f:
                    self._ns = f.read().strip() or "default"
            except OSError:  # pragma: no cover - token exists, namespace gone
                self._ns = "default"
        return self._ns

    def _event(self, msg: str) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(msg)
        except Exception:
            pass  # a broken log hook must never break the election
