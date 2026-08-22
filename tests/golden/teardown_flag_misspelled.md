## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🚨 **Teardown flag misspelled** — a `decommission` / `allowDeletion` key in this PR is not one the platform reads, so it arms nothing (COPS-2707)

---

## ⛔ STOP — teardown flag misspelled in `pv-foo-c`

⛔ **A teardown flag here is misspelled, so it arms nothing.**

| You wrote | The key the platform reads |
|---|---|
| `appspace.decomission` | `appspace.decommission` |

**Fix:** rename the key to `appspace.decommission` in `gcp/prod/private-cloud/na2-a/monthly/pv-foo-c/customer.yaml` and push. Until then nothing is armed on `pv-foo-c`, and a later folder removal would leave every workload running instead of deleting it.

**Everything else in this PR is unreviewed** — no diff, no VM check, no phase table. Fix the key, push, and the full review comes back on the next commit.

---
**Status:** ⛔ TEARDOWN FLAG MISSPELLED — it arms nothing, see above
*2026-01-01 00:00 UTC — acme-diff-preview [permanent] [base:00001111]*