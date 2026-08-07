## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

### 🧭 Merge summary

✅ **Routine** — nothing dangerous detected

- ✅ 1 app(s) change, nothing risk-flagged

---


⚠️ **`pv-hp-a-ms`** — 7 resource(s) changed

> ⬆️ **6 of 7 changed resource(s)** are the version transition `2603.1.9 → 2603.1.10` only. [Full hunks in the full diff view](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345)
>
> Folded lines: image tags, chart labels, version env values, checksums, deploy timestamps.

**`/apps/StatefulSet mongo`**

```diff
--- 
+++ 
@@ -3,8 +3,9 @@
 metadata:
   annotations:
+    cnrm.cloud.google.com/reconcile-interval-in-seconds: "3600"
 spec:
   template:
     spec:
       containers:
-        image: registry.example/mongo:2603.1.9
+        image: registry.example/mongo:2603.1.10
```

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

---
**Status:** ⚠️ 7 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*