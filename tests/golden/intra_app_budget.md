## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

### 🧭 Merge summary

✅ **Routine** — nothing dangerous detected

- ✅ 1 app(s) change, nothing risk-flagged

---


⚠️ **`pv-big-a-ms`** — 12 resource(s) changed

**`/v1/ConfigMap cfg-000`**

```diff
--- 
+++ 
@@ -2,24 +2,24 @@
 data:
-  maxConnections: "100"
+  maxConnections: "200"
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

**`/v1/ConfigMap cfg-001`**

```diff
--- 
+++ 
@@ -2,24 +2,24 @@
 data:
-  maxConnections: "100"
+  maxConnections: "201"
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

**`/v1/ConfigMap cfg-002`**

```diff
--- 
+++ 
@@ -2,24 +2,24 @@
 data:
-  maxConnections: "100"
+  maxConnections: "202"
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

**`/v1/ConfigMap cfg-003`**

```diff
--- 
+++ 
@@ -2,24 +2,24 @@
 data:
-  maxConnections: "100"
+  maxConnections: "203"
   pad-003-00: value
   pad-003-01: value
   pad-003-02: value
   pad-003-03: value
   pad-003-04: value
   pad-003-05: value
   pad-003-06: value
   pad-003-07: value
   pad-003-08: value
   pad-003-09: value
   pad-003-10: value
   pad-003-11: value
   pad-003-12: value
   pad-003-13: value
   pad-003-14: value
   pad-003-15: value
   pad-003-16: value
   pad-003-17: value
   pad-003-18: value
   pad-003-19: value
```

**`/v1/ConfigMap cfg-004`**

```diff
--- 
+++ 
@@ -2,24 +2,24 @@
 data:
-  maxConnections: "100"
+  maxConnections: "204"
   pad-004-00: value
   pad-004-01: value
   pad-004-02: value
   pad-004-03: value
   pad-004-04: value
   pad-004-05: value
   pad-004-06: value
   pad-004-07: value
   pad-004-08: value
   pad-004-09: value
   pad-004-10: value
   pad-004-11: value
   pad-004-12: value
   pad-004-13: value
   pad-004-14: value
   pad-004-15: value
   pad-004-16: value
   pad-004-17: value
   pad-004-18: value
   pad-004-19: value
```

> ✂️ **7 more changed resource(s) omitted** here to keep the comment scannable. None is a deletion, zeroed replica or VM change. [Full hunks for `pv-big-a-ms`](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345#app-pv-big-a-ms)
>
> Omitted: `/v1/ConfigMap cfg-005`, `/v1/ConfigMap cfg-006`, `/v1/ConfigMap cfg-007`, `/v1/ConfigMap cfg-008`, `/v1/ConfigMap cfg-009`, `/v1/ConfigMap cfg-010`, `/v1/ConfigMap cfg-011`

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

---
**Status:** ⚠️ 12 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*