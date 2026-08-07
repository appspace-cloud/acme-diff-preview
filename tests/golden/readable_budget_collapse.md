## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod` | 📦 Large changeset (6 apps)

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

### 🧭 Merge summary

✅ **Routine** — nothing dangerous detected

- ✅ 6 app(s) change, nothing risk-flagged

---


#### Changeset overview

| App | Status | Changed resources | Diff group |
|-----|--------|--------------------|------------|
| `pv-env-0-ms` | ⚠️ changed | 1 | — |
| `pv-env-1-ms` | ⚠️ changed | 1 | — |
| `pv-env-2-ms` | ⚠️ changed | 1 | — |
| `pv-env-3-ms` | ⚠️ changed | 1 | — |
| `pv-env-4-ms` | ⚠️ changed | 1 | — |
| `pv-env-5-ms` | ⚠️ changed | 1 | — |

⚠️ **`pv-env-0-ms`** — 1 resource(s) changed

**`/apps/Deployment broadcast`**

```diff
--- 
+++ 
@@ -10,23 +10,23 @@
     env: pv-big-000
-  replicas: 2
+  replicas: 3
     pad-000-00: value
     pad-000-01: value
     pad-000-02: value
     pad-000-03: value
     pad-000-04: value
     pad-000-05: value
     pad-000-06: value
     pad-000-07: value
     pad-000-08: value
     pad-000-09: value
     pad-000-10: value
     pad-000-11: value
     pad-000-12: value
     pad-000-13: value
     pad-000-14: value
     pad-000-15: value
     pad-000-16: value
     pad-000-17: value
     pad-000-18: value
     pad-000-19: value
```

⚠️ **`pv-env-1-ms`** — 1 resource(s) changed

**`/apps/Deployment broadcast`**

```diff
--- 
+++ 
@@ -10,23 +10,23 @@
     env: pv-big-001
-  replicas: 2
+  replicas: 3
     pad-001-00: value
     pad-001-01: value
     pad-001-02: value
     pad-001-03: value
     pad-001-04: value
     pad-001-05: value
     pad-001-06: value
     pad-001-07: value
     pad-001-08: value
     pad-001-09: value
     pad-001-10: value
     pad-001-11: value
     pad-001-12: value
     pad-001-13: value
     pad-001-14: value
     pad-001-15: value
     pad-001-16: value
     pad-001-17: value
     pad-001-18: value
     pad-001-19: value
```

⚠️ **`pv-env-2-ms`** — 1 resource(s) changed

**`/apps/Deployment broadcast`**

```diff
--- 
+++ 
@@ -10,23 +10,23 @@
     env: pv-big-002
-  replicas: 2
+  replicas: 3
     pad-002-00: value
     pad-002-01: value
     pad-002-02: value
     pad-002-03: value
     pad-002-04: value
     pad-002-05: value
     pad-002-06: value
     pad-002-07: value
     pad-002-08: value
     pad-002-09: value
     pad-002-10: value
     pad-002-11: value
     pad-002-12: value
     pad-002-13: value
     pad-002-14: value
     pad-002-15: value
     pad-002-16: value
     pad-002-17: value
     pad-002-18: value
     pad-002-19: value
```

> ✂️ **3 more changed app(s) omitted here to keep this comment scannable.** Every omitted diff is ordinary (no deletions, downgrades, zeroed replicas or VM changes) — read them in full in the [full diff view](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345). Omitted: pv-env-3-ms, pv-env-4-ms, pv-env-5-ms

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

---
**Status:** ⚠️ 6 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*