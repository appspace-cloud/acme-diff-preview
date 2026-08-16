## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🖥⛔ **Decommission arming is BROKEN** — this PR strips the Linux VM config in the same change that arms deletion, so the live VM, disk and IP would be pruned under `abandon` and ORPHANED in the cloud, not deleted. Keep the VM block and only add `allowDeletion`.

---

## 🔒⚠️ DECOMMISSION ARMED for `pv-foo-c` ⚠️🔒

**`appspace.decommission: true` was added — but this PR does NOT follow the decommission flow: it strips the VM config it is arming.** See the warning below the table.

| Phase | State | What it does |
|-------|-------|--------------|
| **Phase 1 — arm VM deletion** | ⛔ **broken by this PR** | `deployLinuxServicesK8s.defaults.allowDeletion` lets the cascade delete the real VM, its data disk and its reserved IP; without it they survive under the abandon policy |
| **Phase 2 — arm cascade** | ✅ **this PR** | `appspace.decommission` makes the Applications eligible for the cascade-delete finalizer — `decommissionPurgeData` is not armed, so the BigQuery dataset and the content bucket are abandoned and stay recoverable (backup bucket is always abandoned) |
| **Phase 3 — remove folder** | ⬜ pending | a later PR deletes the Applications and every resource they manage, Config Connector cloud resources included; that PR gets its own full inventory panel |

## 🖥⛔ VM CONFIG STRIPPED WHILE ARMING DECOMMISSION

**This PR removes the Linux VM config for `pv-foo-c` in the same change that arms its deletion.**

Helm stops rendering the VM resources the moment this merges, ArgoCD prunes them, and the live objects go out under their current `deletion-policy: abandon` — the real VM, its data disk and its reserved IP are **orphaned in the cloud, not deleted**.

Stripped in this PR: `deployLinuxServicesK8s.defaults.allowDeletion`, `deployLinuxServicesK8s.enabled`, `deployLinuxServicesK8s.instances.svc-a.enabled`

**Fix:** keep the existing `deployLinuxServicesK8s` block exactly as it is and only add `defaults.allowDeletion: true`. The block can be removed after the cascade has actually deleted the VM (Phase 3).

---

✅ **`pv-foo-c-glb`** — no manifest changes

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ✅ No manifest changes | ⛔ DECOMMISSION ARMING BROKEN — the VM would be orphaned, see comment
*2026-01-01 00:00 UTC — acme-diff-preview [permanent] [base:00001111]*