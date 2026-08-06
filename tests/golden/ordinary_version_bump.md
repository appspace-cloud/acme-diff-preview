## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

### 🧭 Merge summary

✅ **Routine** — nothing dangerous detected

- ✅ 1 app(s) change, nothing risk-flagged

---


⚠️ **`pv-acme-a-ms`** — 1 resource(s) changed

**`/apps/Deployment broadcast`**

```diff
--- 
+++ 
@@ -10,7 +10,7 @@
     spec:
       containers:
-        image: appspace-ms:2603.0.0
+        image: appspace-ms:2603.1.0
         ports:
```

✅ **`pv-acme-a-ss`** — no manifest changes

---
**Status:** ⚠️ 1 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*