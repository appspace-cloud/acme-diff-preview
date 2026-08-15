## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🗑️ **Environment decommission** — resources are deleted; data is abandoned, not purged

---

# 🗑️⚠️ ENVIRONMENT DECOMMISSION ⚠️🗑️

**`pv-foo-c` is being deleted by this PR (was running chart version `2603.1.0`). This is a destructive, hard-to-reverse change — verify this is intentional.**

| Phase | State | What it does |
|-------|-------|--------------|
| **Phase 1 — arm VM deletion** | — not applicable | this environment declares no `deployLinuxServicesK8s` VMs, so there is nothing to arm |
| **Phase 2 — arm cascade** | ✅ **done** | `appspace.decommission` makes the Applications eligible for the cascade-delete finalizer — `decommissionPurgeData` is not armed, so the BigQuery dataset and the content bucket are abandoned and stay recoverable |
| **Phase 3 — remove folder** | ✅ **this PR** | deletes the Applications and every resource they manage, Config Connector cloud resources included — the destructive step, and this PR is it |

✅ **Data is not purged.** The BigQuery dataset and the content bucket are abandoned rather than deleted, so they survive in GCP and stay recoverable. Destroying them needs `appspace.decommissionPurgeData: true` as a separate, reviewed change (COPS-2572).

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