## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

✅ **Routine** — nothing dangerous detected

- ✅ No manifest changes and no risky configuration change

---

## 🔒 DECOMMISSION PHASE 1 for `pv-foo-c`

**`allowDeletion` was armed. This PR deletes nothing by itself.** It flips this environment's Linux VM, its data disk and its reserved IP from `deletion-policy: abandon` to `delete`, so a later cascade can remove them in GCP instead of leaving them behind.

| Phase | State | What it does |
|-------|-------|--------------|
| **Phase 1 — arm VM deletion** | ✅ **this PR** | `deployLinuxServicesK8s.defaults.allowDeletion` lets the cascade delete the real VM, its data disk and its reserved IP; without it they survive under the abandon policy |
| **Phase 2 — arm cascade** | ⬜ pending | `appspace.decommission` makes the Applications eligible for the cascade-delete finalizer — `decommissionPurgeData` is not armed, so the BigQuery dataset and the content bucket are abandoned and stay recoverable (backup bucket is always abandoned) |
| **Phase 3 — remove folder** | ⬜ pending | a later PR deletes the Applications and every resource they manage, Config Connector cloud resources included; that PR gets its own full inventory panel |

**Next:** arm the cascade with `appspace.decommission: true` (Phase 2), let it sync, then remove the environment folder in its own PR (Phase 3). Phases 1 and 2 may share a PR; Phase 3 must not.

---

✅ **`pv-foo-c-glb`** — no manifest changes

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ✅ No manifest changes
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*