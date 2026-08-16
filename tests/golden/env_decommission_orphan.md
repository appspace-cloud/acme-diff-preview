## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🗑️ **Environment decommission** — no cascade armed: the Applications are removed but their workloads keep running, orphaned and unmanaged

---

# 🗑️⚠️ ENVIRONMENT DECOMMISSION ⚠️🗑️

**`pv-foo-c` is being deleted by this PR (was running chart version `2603.1.0`). This is a destructive, hard-to-reverse change — verify this is intentional.**

| Phase | State | What it does |
|-------|-------|--------------|
| **Phase 1 — arm VM deletion** | — not applicable | this environment declares no `deployLinuxServicesK8s` VMs, so there is nothing to arm |
| **Phase 2 — arm cascade** | ⬜ pending | `appspace.decommission` makes the Applications eligible for the cascade-delete finalizer — `decommissionPurgeData` is not armed, so the BigQuery dataset and the content bucket are abandoned and stay recoverable (backup bucket is always abandoned) |
| **Phase 3 — remove folder** | ✅ **this PR** | deletes the Applications and every resource they manage, Config Connector cloud resources included — the destructive step, and this PR is it |

⚠️ **The ArgoCD Application is removed, but its resources are NOT deleted — they keep running.**

This environment has not opted into cascade deletion, and the ApplicationSet sets `preserveResourcesOnDeletion: true`, so every workload below is left orphaned in the cluster: still running, still costing money, still holding IPs and disks, and no longer managed by ArgoCD.

To delete them together with the Application, set `appspace.decommission: true` in the environment's `customer.yaml` and let it sync BEFORE the folder is removed (COPS-2539). Otherwise they have to be cleaned up by hand.

- **Resources that will be LEFT RUNNING (orphaned):** 9 total — 3 Namespace, 3 Service, 3 apps/Deployment
- **Workloads left running:** `pv-foo-c-glb-web`, `pv-foo-c-ms-web`, `pv-foo-c-ss-web`

---

#### Changeset overview

| App | Status | Changed resources | Diff group |
|-----|--------|--------------------|------------|
| `pv-foo-c-glb` | 🗑️ decommissioned | — | — |
| `pv-foo-c-ms` | 🗑️ decommissioned | — | — |
| `pv-foo-c-ss` | 🗑️ decommissioned | — | — |

🗑️ **`pv-foo-c-glb`** — environment decommissioned (see warning above)

🗑️ **`pv-foo-c-ms`** — environment decommissioned (see warning above)

🗑️ **`pv-foo-c-ss`** — environment decommissioned (see warning above)

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ✅ No manifest changes
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*