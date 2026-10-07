# Exact-head worker qualification

This source successor starts from worker
`72433d338b79b97f0e8ebe1cfdacb0e1238ea655`. It adds an admission proof to the
authenticated idempotent Stop endpoint. The proof hashes the normalized key and
the original UTF-8 HTTP body and names the reserved root. A conflicting request
or failed authentication cannot receive a proof.

The hosted portable lane exercises ordinary and supervised admission before
create, during admission, and after restart. The native lane retains all existing
cases, deadlines, durable Stop fences, cgroup settlement, and source bindings.
The receipt requires at least the accepted 176-case roster and has no mandatory
failures, errors, skips, duplicate cases, or retries.

High-memory qualification requires an organization-owned GitHub larger runner
with at least 64 GiB. Set `QUALIFICATION_LARGER_RUNNER` to its configured name and
provide `HOSTED_RUNNER_READ_TOKEN` with supported organization runner-read access.
The readiness job reads provider state and repository access before scheduling.
It records unavailable capacity as a failure. There is no local or smaller-runner
fallback for this gate. The personal-account fork currently lacks this owner
prerequisite. The portable lane can still provide separate source evidence.

All required artifacts use 31 days of native GitHub retention. The final job
reads actual creation and expiry timestamps and SHA-256 digests. Receipts remain
unqualified until independent exact-source acceptance. Historical failed and
accepted revisions retain their own disposition. This workflow grants no worker
enablement, dispatch, composition, recovery execution, or deployment authority.
