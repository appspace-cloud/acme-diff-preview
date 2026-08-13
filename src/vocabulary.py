"""The vocabulary of diff outcomes and failure reasons (COPS-2658 phase 2).

Every part of the service speaks in these terms: what a diff attempt
produced, why it failed, and whether that failure is worth retrying or
permanently blocks the pull request. Extracted verbatim from
diff_preview.py so that both the hub and the render modules can share one
definition instead of one importing the other.

A leaf: it imports nothing from the service and must stay that way.
"""


OUT_DIFF          = "diff"
OUT_NO_DIFF       = "no_diff"
OUT_INDETERMINATE = "indeterminate"
OUT_ERROR         = "error"
OUT_DECOMMISSIONED = "decommissioned"


REASON_OCI_NOT_FOUND = "oci_not_found"      # version absent in registry — PERMANENT, blocks PR
REASON_OCI_PULL      = "oci_pull_failed"    # transient pull/login failure — retry
REASON_METADATA      = "metadata_pending"   # app not yet in the 5-min app cache — retry
REASON_RENDER        = "render_failed"      # `helm template` failed (bad values/chart) — soft
REASON_TIMEOUT       = "timeout"            # a step exceeded DIFF_TIMEOUT — retry


REASON_UNEXPECTED    = "unexpected_error"


REASON_INVALID_VERSION = "invalid_version"
REASON_NAME_TOO_LONG   = "name_too_long"      # COPS-2552: derived GCP service account name rejected


REASON_INVALID_YAML  = "invalid_yaml"
REASON_MISSING_REQUIRED = "missing_required"  # v2.6.2: helm `required`/nil-deref on absent value
REASON_SCHEMA_INVALID   = "schema_invalid"     # COPS-2554: values.schema.json validation failed


RETRYABLE_REASONS = {REASON_OCI_PULL, REASON_METADATA, REASON_TIMEOUT, REASON_RENDER}
# Reasons that permanently block the PR (the deployer would fail the same way).
PERMANENT_REASONS = {REASON_OCI_NOT_FOUND, REASON_INVALID_VERSION,
                     REASON_INVALID_YAML, REASON_MISSING_REQUIRED,
                     REASON_SCHEMA_INVALID, REASON_NAME_TOO_LONG}
