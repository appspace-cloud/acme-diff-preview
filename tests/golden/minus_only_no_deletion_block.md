## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

✅ **Routine** — nothing dangerous detected

- ✅ 1 app(s) change, nothing risk-flagged

---


#### Changeset overview

| App | Status | Changed resources | Diff group |
|-----|--------|--------------------|------------|
| `pv-acme-a-ms` | ⚠️ changed | 6 | — |

⚠️ **`pv-acme-a-ms`** — 6 resource(s) changed

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

> ♻️ **5 more resource(s) change exactly the same lines.**
>
> Same change: `/apps/Deployment svc-1`, `/apps/Deployment svc-2`, `/apps/Deployment svc-3`, `/apps/Deployment svc-4`, `/apps/Deployment svc-5`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ⚠️ 6 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*