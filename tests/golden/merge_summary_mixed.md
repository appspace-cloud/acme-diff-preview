## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod` | 📦 Large changeset (6 apps)

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

### 🧭 Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (2 item(s))

- 🖥️ **VM infrastructure change flagged dangerous** — see the VM section
- ❌ **1 resource(s) deleted** in 1 app(s): pv-zzz-risky
- ⬆️ **4 environment(s) jumping** `image`: `appspace-ms:2603.0.0` → `appspace-ms:2603.1.0`: pv-fleet-00, pv-fleet-01, pv-fleet-02, pv-fleet-03

---

## 🖥️🚨 VM INFRASTRUCTURE CHANGES 🚨

**This PR touches virtual machine infrastructure (KCC linux-services). A botched VM change is slow and painful to recover from — verify every line below before merging.**

- 🚨 `pv-vm-a` · `ComputeInstance pv-acme-svc-a`: `machineType` `n2d-standard-4` → `n2d-standard-8` — machineType changes while the VM is not parked TERMINATED — the runbook requires stopping the VM first

## 🗑️⚠️ 1 RESOURCE(S) DELETED ⚠️

**This PR removes the following resources entirely. Verify each deletion is intentional — 🔐-flagged kinds can revoke access or destroy credentials/data.**

- `pv-zzz-risky-ms` → `/v1/Service gone`

---

#### Changeset overview

| App | Status | Changed resources | Diff group |
|-----|--------|--------------------|------------|
| `pv-fleet-00-ms` | ⚠️ changed | 1 | — |
| `pv-fleet-01-ms` | ⚠️ changed | 1 | — |
| `pv-fleet-02-ms` | ⚠️ changed | 1 | — |
| `pv-fleet-03-ms` | ⚠️ changed | 1 | — |
| `pv-vm-a-ss` | ⚠️ changed | 1 | — |
| `pv-zzz-risky-ms` | ⚠️ changed | 1 | — |

> ⬆️ **Routine version bump** `image`: `appspace-ms:2603.0.0` → `appspace-ms:2603.1.0` — **4 environments**: pv-fleet-00, pv-fleet-01, pv-fleet-02, pv-fleet-03 — full diffs in the [full diff view](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345)

⚠️ **`pv-vm-a-ss`** — 1 resource(s) changed

[Full hunks in the full diff view](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345)

⚠️ **`pv-zzz-risky-ms`** — 1 resource(s) changed

[Full hunks in the full diff view](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345)

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

---
**Status:** ⚠️ 6 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*