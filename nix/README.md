# Native lifecycle qualification

The hosted `checks.x86_64-linux.target-job-lifecycle` gate runs the real packaged
Hermes environment against a disposable systemd worker, with local, SSH, and
cross-UID authority cases. The driver deadline is 900 seconds. Test assertions
keep their own deadlines. The gate requires every mandatory case and rejects
failures, errors, skips, duplicate cases, and JUnit retry/flaky markers.

Successful builds retain `lifecycle.xml` and a SHA-256-bound `receipt.json`.
Failed Nix derivations can discard copied driver outputs. After a completed
suite, the driver therefore retrieves bounded, indexed report and source frames
and prints them synchronously after pytest diagnostics, before enforcing the
original exit-status assertion. This prevents VM console-reader cleanup races
and places complete report frames at the tail of the failed driver log. The
generated workflow retains those exact bytes in its `build.log`, including
after failure, for at least 31 days. Uploads request 32 days because provider
creation and expiry timestamps can shorten a nominal interval by a few seconds.

To recover a completed report from downloaded build evidence:

```sh
SIMIT_NIX_BUILD_RESULTS=/absolute/evidence-directory \
  python3 scripts/qualify-job-lifecycle.py retain-diagnostics
```

The collector verifies every chunk, byte count, SHA-256 digest, XML document,
and source revision against the artifact's checkout revision. It rejects mixed,
corrupt, incomplete, or mismatched evidence. An unstarted or interrupted suite
does not become a completed report. `diagnostics.json` always has
`qualified: false`; diagnostic recovery never creates a qualification receipt.
The generated workflow uses `post_build_always` to recover completed diagnostic
frames before artifact upload, preserving a failed build's exit status. Its
drift check pins qualified immutable Simit
`bbfef6f6674d9977d7504a0e9c66be39147d9400`. The
`Generate native diagnostic CI` PR workflow builds that generator on the hosted
runner and retains config/workflow files with source and digest bindings. The
adopted bytes must match the source-bound hosted generation artifact exactly.
Hosted proof of automatic failed-build collection is still required; successful
generation and manual recovery do not qualify the native lifecycle.

The test-only diagnostics plugin also prints aggregated real control latency,
authority-lock wait, and locked-dispatch duration, plus fixed operation labels
and separate local/SSH ownership-case aggregates. It keeps original calls and
return values and prints no commands, payloads, paths, credentials, or claim IDs.
These timings help explain deadline failures; they do not relax qualification.

## Supervised kernel lifecycle

Supervised kernel setup stages its runner, tool stubs, and private environment
in one admitted job, followed by the original fenced interpreter launch. File
contents still travel only on stdin, with owner-only directories and files.
This reduces serialized authority/control round trips within the existing
ownership deadlines; it does not bypass job admission or settlement.

Process-output drains consume one fresh state/exit/output frame at their current
offset. A short read of the regular target log proves its current EOF; a full
64-KiB frame requires another observation before completion. Streaming readers
do not share the legacy split-query cache lock. Unknown target state still holds
ownership, and descendant-wait handles require a settled cgroup.
