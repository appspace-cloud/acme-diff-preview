## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- ⛔ **An AEC clone starts running with a copy of production data (zeroPods true to false)** in `pv-x--aec1-a` - to merge anyway, add `Confirm-Clone-Sanitized: pv-x--aec1-a` to a commit message

---

## 🚨 AEC CLONE STARTS RUNNING

- `pv-x--aec1-a` starts running with this PR (`zeroPods` goes from `true` to `false`).

A clone starts with a copy of the production data, so it can call the customer live integrations: SSO, webhooks, mail and connected apps (AE-15507). `zeroPods` does not stop the Core VM.

Before you merge, check on the clone Mongo that these are empty, and write the counts in the PR:

- `passport.passports`
- `mail.smtpConfigurations`
- `integrationwebhook.webhookSubscriptions`
- `authorization.authorizationregistrations`
- the `applicationintegration` database
- `cacsJwts` in `feedconnector`, `contentconversion`, `userinbox` and `mention`

Runbook: https://appspace.atlassian.net/wiki/spaces/cops/pages/66093626 step 2.

Then confirm with an empty commit:

```
git commit --allow-empty -m "Confirm-Clone-Sanitized: pv-x--aec1-a"
git push
```

---

#### Changeset overview

| App | Status | Changed resources | Diff group |
|-----|--------|--------------------|------------|
| `pv-x--aec1-a-ms` | ⚠️ changed | 1 | — |

⚠️ **`pv-x--aec1-a-ms`** — 1 resource(s) changed

**`/apps/Deployment pv-x--aec1-a/api-gateway`**

```diff
-  replicas: 0
+  replicas: 2
```

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ⚠️ 1 resource(s) will change | ⛔ Blocked - An AEC clone starts running with a copy of production data (zeroPods true to false) in pv-x--aec1-a. To merge anyway, add 'Confirm-Clone-Sanitized: pv-x--aec1-a' to a commit message (see PR comment)
*2026-01-01 00:00 UTC — acme-diff-preview [blocked] [base:00001111]*