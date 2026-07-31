## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

---

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

**`/apps/Deployment svc-1`**

```diff
--- 
+++ 
@@ -20,7 +20,6 @@
     app.kubernetes.io/name: broadcast
 spec:
   
-  replicas: 2
   
   strategy:
```

**`/apps/Deployment svc-2`**

```diff
--- 
+++ 
@@ -20,7 +20,6 @@
     app.kubernetes.io/name: broadcast
 spec:
   
-  replicas: 2
   
   strategy:
```

**`/apps/Deployment svc-3`**

```diff
--- 
+++ 
@@ -20,7 +20,6 @@
     app.kubernetes.io/name: broadcast
 spec:
   
-  replicas: 2
   
   strategy:
```

**`/apps/Deployment svc-4`**

```diff
--- 
+++ 
@@ -20,7 +20,6 @@
     app.kubernetes.io/name: broadcast
 spec:
   
-  replicas: 2
   
   strategy:
```

**`/apps/Deployment svc-5`**

```diff
--- 
+++ 
@@ -20,7 +20,6 @@
     app.kubernetes.io/name: broadcast
 spec:
   
-  replicas: 2
   
   strategy:
```

---
**Status:** ⚠️ 6 resource(s) will change
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*