## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🔒 **Decommission ARMED** — this environment becomes eligible for cascade deletion when its folder is removed

---

## 🔒⚠️ DECOMMISSION ARMED for `pv-foo-c` ⚠️🔒

**`appspace.decommission: true` was added. This PR deletes nothing by itself.** `pv-foo-c-glb`, `pv-foo-c-ms`, `pv-foo-c-ss` become eligible for the cascade-delete finalizer, which only acts when this environment's folder is removed in a later PR.

| Phase | State | What it does |
|-------|-------|--------------|
| **Phase 1 — arm VM deletion** | — not applicable | this environment declares no `deployLinuxServicesK8s` VMs, so there is nothing to arm |
| **Phase 2 — arm cascade** | ✅ **this PR** | `appspace.decommission` makes the Applications eligible for the cascade-delete finalizer — `decommissionPurgeData` is not armed, so the BigQuery dataset and the content bucket are abandoned and stay recoverable |
| **Phase 3 — remove folder** | ⬜ pending | a later PR deletes the Applications and every resource they manage, Config Connector cloud resources included; that PR gets its own full inventory panel |

**Nothing changes for `pv-foo-c` until Phase 3:** every workload keeps running, disks stay held, costs keep accruing and the environment is still managed by ArgoCD.

Even a full cascade leaves the content backup bucket and some namespace-level leftovers behind. Full procedure: see `acme-components` `documentation/`.

---

✅ **`pv-foo-c-glb`** — no manifest changes

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ✅ No manifest changes
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*