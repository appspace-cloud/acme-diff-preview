## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🚨 **Teardown flag misspelled** — a `decommission` / `allowDeletion` key in this PR is not one the platform reads, so it arms nothing (COPS-2707)

---

## 🚨 TEARDOWN FLAG MISSPELLED for `pv-foo-c` 🚨

🚨 **A teardown flag here is misspelled, so it arms nothing.**

Helm and the ApplicationSet `templatePatch` look the key up by its exact name. A near miss is not read, not defaulted and not warned about anywhere else: the environment renders byte-identically, so this PR would otherwise merge as a no-op.

| In this PR | The key the platform reads |
|---|---|
| `appspace.decomission` | `appspace.decommission` |

**Nothing is armed for `pv-foo-c`.** Fix the spelling in this PR. If a later PR removes this environment's folder believing the cascade is on, every workload is left running and unmanaged instead of deleted.

---

✅ **`pv-foo-c-glb`** — no manifest changes

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ✅ No manifest changes
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*