## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-dev`

## ℹ️ Merge summary

✅ **Routine** — nothing dangerous detected

- 🆕 **New environment** in this PR

---

### 🆕 New Environment(s) Detected

This PR adds configuration for **1 new environment(s)** that do not yet exist in ArgoCD. The ApplicationSet will create them automatically after merge.

#### `pv-new-x-a` (chart `2603.0.1-dev`)

**Files added:**

- `gcp/dev/private-cloud/ap1/custom/pv-new-x-a/customer.yaml`

🚀 **A completely new environment will be provisioned from scratch.** The full rendered output is too large to display here, so this is a summary of what will be created on merge:

- **Chart version:** `2603.0.1-dev`
- **Resources:** 3 total — 1 ConfigMap, 1 Deployment, 1 Service
- **Applications (1):** `api-gateway`

---
### 📄 Full rendered output

The complete redacted manifest of everything that will be created on merge, kept for traceability. If Bitbucket truncates this comment, the untruncated output stays available through the build status link.

#### `pv-new-x-a` — chart `2603.0.1-dev`, 3 resource(s)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-gateway
---
apiVersion: v1
kind: Service
metadata:
  name: api-gateway
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-config
data:
  region: ap1
```

---
**Status:** ✅ New environment(s) - all resources will be created on merge
*2026-01-01 00:00 UTC — acme-diff-preview [clean] [base:00001111]*