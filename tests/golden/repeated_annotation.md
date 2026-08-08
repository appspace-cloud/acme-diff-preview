## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

### 🧭 Merge summary

✅ **Routine** — nothing dangerous detected

- ✅ 1 app(s) change, nothing risk-flagged

---


⚠️ **`pv-acme-a-glb`** — 12 resource(s) changed

**`/iam.cnrm.cloud.google.com/IAMPolicyMember member-000`**

```diff
--- 
+++ 
@@ -2,6 +2,7 @@
 metadata:
   annotations:
+    cnrm.cloud.google.com/reconcile-interval-in-seconds: "3600"
   labels:
     app: appspace
```

> ♻️ **11 more resource(s) change exactly the same lines.**
>
> Same change: `/iam.cnrm.cloud.google.com/IAMPolicyMember member-001`, `/iam.cnrm.cloud.google.com/IAMPolicyMember member-002`, `/iam.cnrm.cloud.google.com/IAMPolicyMember member-003`, `/iam.cnrm.cloud.google.com/IAMPolicyMember member-004`, ...

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

---
**Status:** ⚠️ 12 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*