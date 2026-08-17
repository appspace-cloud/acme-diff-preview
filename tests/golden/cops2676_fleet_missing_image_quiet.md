## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

## ℹ️ Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (2 item(s))

- ❌ **2 resource(s) deleted** in 2 environment(s): pv-advocate-b, pv-aexp-a
- ⛔ **4 environment(s) cannot render** — **Missing Image Tag on => platform**: pv-adl-a, pv-asi-b, pv-atea-a, pv-ato-c
- ⬆️ **2 environment(s) bump** `2603.0.19` → `2603.2.0`: pv-advocate-b, pv-aexp-a

---

## ⚙️ RENDER BLOCKED

❌ **4 environments cannot render** — ⚙️ **MISSING REQUIRED VALUE**
> **Missing Image Tag on => platform**
> Chart template: `templates/configmaps/micro-versions-info.yaml:16`
> **Fix:** add the missing value to this environment's `customer.yaml`, or to the `config.yaml` of its cohort or ring if every environment at that level needs it.

> pv-adl-a, pv-asi-b, pv-atea-a, pv-ato-c

> *Other comment sections are collapsed while render is blocked. Open the full-diff page for deletions / bumps / per-app detail.*

---

## 🗑️ 2 resource(s) also deleted (details collapsed while render is blocked)

[Full hunks in the full diff view](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345)

---

#### Changeset overview collapsed (6 apps) — render is blocked

[Full hunks in the full diff view](https://diffs.appspace.example/diff/acme-config-prod/42/abc12345)

🔎 **Full rendered diff (every hunk):** https://diffs.appspace.example/diff/acme-config-prod/42/abc12345

---
**Status:** ⚠️ 214 resource(s) will change — ❔ 4 app(s) could not be evaluated (diff unavailable, NOT confirmed unchanged)
*2026-01-01 00:00 UTC — acme-diff-preview [permanent] [base:00001111]*