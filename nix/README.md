# Native lifecycle qualification

The hosted `checks.x86_64-linux.target-job-lifecycle` gate runs the real packaged
Hermes environment against a disposable systemd worker, with local, SSH, and
cross-UID authority cases. The driver deadline is 900 seconds. Test assertions
keep their own deadlines. The gate requires every mandatory case and rejects
failures, errors, skips, duplicate cases, and JUnit retry/flaky markers.

Successful builds retain `lifecycle.xml` and a SHA-256-bound `receipt.json`.
Failed Nix derivations can discard copied driver outputs. After a completed
suite, the driver therefore emits bounded, indexed report and source frames to
the VM console before enforcing the original exit-status assertion. The
generated workflow retains those exact bytes in its `build.log`, including
after failure, for 31 days.

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
Simit's optional `post_build_always` collector can automate recovery before
artifact upload once its generator revision is qualified and pinned.

The test-only diagnostics plugin also prints aggregated real control latency,
authority-lock wait, and locked-dispatch duration. It keeps original calls and
return values and prints no commands, payloads, paths, credentials, or claim IDs.
These timings help explain deadline failures; they do not relax qualification.
