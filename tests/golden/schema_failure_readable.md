## 🔭 ACME Diff Preview

**Commit** `abc12345` → `main` | `acme-config-prod`

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

### 🧭 Merge summary

⛔ **DO NOT MERGE** without checking the item(s) below (1 item(s))

- ⛔ **1 environment(s) cannot render** — helm failed here and the deployer will fail the same way: pv-glencore-c

---


❌ **`pv-glencore-c-ms`** — ⚙️ **SCHEMA VALIDATION FAILED — this environment's values violate the chart's schema**
> at '/appspace/microservices/definitions/svc-00': got null, want object
> at '/appspace/microservices/definitions/svc-01': got null, want object
> at '/appspace/microservices/definitions/svc-02': got null, want object
> at '/appspace/microservices/definitions/svc-03': got null, want object
> at '/appspace/microservices/definitions/svc-04': got null, want object
> at '/appspace/microservices/definitions/svc-05': got null, want object
> at '/appspace/microservices/definitions/svc-06': got null, want object
> at '/appspace/microservices/definitions/svc-07': got null, want object
> at '/appspace/microservices/definitions/svc-08': got null, want object
> at '/appspace/microservices/definitions/svc-09': got null, want object
> *... and 43 more violation(s) of the same kind*
> **Why:** 53 of these are `null`, which is what YAML gives a key whose body was deleted or commented out.
> **Fix:** write an explicit empty map to keep the entry with pure chart defaults, for example `myservice: {}`.
> ⚠️ Do **not** delete the key instead: the chart renders one microservice per entry under `microservices.definitions`, so removing it deletes that microservice from the environment.
> **Fix:** correct each value listed above in this environment's `customer.yaml` (or the `config.yaml` of its cohort or ring if every environment needs the fix).

⚠️ The full-diff page could not be produced for this run, so every hunk is inlined below.

---
**Status:** ❔ Diff incomplete — 1 app(s) could not be evaluated (NOT confirmed unchanged)
*2026-01-01 00:00 UTC — acme-diff-preview [permanent] [base:00001111]*