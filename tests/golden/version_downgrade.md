## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

### 🧭 Merge summary

⚠️ **Review before merging** (1 item(s))

- ⬇️ **Chart version downgrade** in pv-acme-a

---

# 🔻⚠️ CHART VERSION DOWNGRADE ⚠️🔻

**This PR moves the chart to a LOWER version. Downgrades can break schema/data migrations that do not run backwards. Verify this is intentional before merging.**

### 🔻 `pv-acme-a-ms`: `2603.1.0` → **`2603.0.0`**

---

⚠️ **`pv-acme-a-ms`** — 1 resource(s) changed

**`/apps/Deployment b`**

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

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ⚠️ 1 resource(s) will change | 🔻 CHART DOWNGRADE — verify intentional
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*