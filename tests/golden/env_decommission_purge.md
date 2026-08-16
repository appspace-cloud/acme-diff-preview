## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🗑️ **Environment decommission** — data purge is ARMED: buckets/datasets are destroyed, not abandoned

---

# 🗑️⚠️ ENVIRONMENT DECOMMISSION ⚠️🗑️

**`pv-foo-c` is being deleted by this PR (was running chart version `2603.1.0`). This is a destructive, hard-to-reverse change — verify this is intentional.**

| Phase | State | What it does |
|-------|-------|--------------|
| **Phase 1 — arm VM deletion** | — not applicable | this environment declares no `deployLinuxServicesK8s` VMs, so there is nothing to arm |
| **Phase 2 — arm cascade** | ✅ **done** | `appspace.decommission` makes the Applications eligible for the cascade-delete finalizer — with `decommissionPurgeData` armed the cascade will **permanently destroy** the BigQuery dataset and the user content bucket (soft-delete off on content; backup bucket always abandoned), not just abandon them |
| **Phase 3 — remove folder** | ✅ **this PR** | deletes the Applications and every resource they manage, Config Connector cloud resources included — the destructive step, and this PR is it |

🚨 **DATA WILL BE PERMANENTLY DESTROYED.** This environment also has `appspace.decommissionPurgeData: true`, so Config Connector empties and deletes the BigQuery dataset and the user content bucket as part of the cascade. **That data is not recoverable afterwards.**

Soft-delete on the **content** bucket is turned off (`softDeletePolicy.retentionDurationSeconds: 0`) so `force-destroy` can complete. The **content backup** bucket always keeps `deletion-policy: abandon` and is left behind on purpose — destroy it by hand after Phase 3 if needed.

- **Resources that will be removed:** 6 total — 3 Service, 3 apps/Deployment
- **Workloads removed:** `pv-foo-c-glb-web`, `pv-foo-c-ms-web`, `pv-foo-c-ss-web`
- **Retained (ArgoCD will NOT delete these):** 3 Namespace (helm.sh/resource-policy: keep)

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