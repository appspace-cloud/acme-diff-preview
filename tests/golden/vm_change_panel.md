## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

### 🧭 Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- 🖥️ **VM infrastructure change flagged dangerous** — see the VM section

---

## 🖥️🚨 VM INFRASTRUCTURE CHANGES 🚨

**This PR touches virtual machine infrastructure (KCC linux-services). A botched VM change is slow and painful to recover from — verify every line below before merging.**

- 🚨 `pv-acme-a` · **linux VM (KCC) · svc**: **added** `svc.allowDeletion` = `True` — deletion-policy flips to `delete` and deletionProtection turns off for this role's VM, disk and address — the next cascade can destroy them in GCP
- 🚨 `pv-acme-a` · **linux VM (KCC) · svc**: `svc.machineType`: `n2d-standard-4` → `n2d-standard-8` — machineType changes while desiredStatus is not TERMINATED — the runbook requires stopping the VM first
- 🚨 `pv-acme-a` · `ComputeInstance pv-acme-svc-a`: `machineType` `n2d-standard-4` → `n2d-standard-8` — machineType changes while the VM is not parked TERMINATED — the runbook requires stopping the VM first

---

⚠️ **`pv-acme-a-ss`** — 1 resource(s) changed

**`/compute.cnrm.cloud.google.com/ComputeInstance pv-acme-a/pv-acme-svc-a`**

```diff
--- 
+++ 
@@ -40,9 +40,9 @@
   resourceID: "pv-acme-svc-a"
   zone: "us-central1-a"
-  machineType: "n2d-standard-4"
+  machineType: "n2d-standard-8"
   canIpForward: false
   desiredStatus: "RUNNING"
```

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ⚠️ 1 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*