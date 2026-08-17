## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- ❌ **1 resource(s) deleted** in 1 environment(s): pv-acme-a

---

## 🗑️⚠️ 1 RESOURCE(S) DELETED ⚠️

**This PR removes the following resources entirely. Verify each deletion is intentional — 🔐-flagged kinds can revoke access or destroy credentials/data.**

- `pv-acme-a-ms` → `/v1/Service gone`

---

#### Changeset overview

| App | Status | Changed resources | Diff group |
|-----|--------|--------------------|------------|
| `pv-acme-a-ms` | ⚠️ changed | 2 | — |

⚠️ **`pv-acme-a-ms`** — 2 resource(s) changed

**`/v1/Service gone`**

```diff
--- 
+++ 
@@ -1,5 +0,0 @@
-apiVersion: v1
-kind: Service
-metadata:
-  name: gone
-spec: {}
```

**`/apps/Deployment svc-0`**

```diff
--- 
+++ 
@@ -20,7 +20,6 @@
     app.kubernetes.io/name: broadcast
 spec:
   
-  replicas: 2
   
   strategy:
```

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ⚠️ 2 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*