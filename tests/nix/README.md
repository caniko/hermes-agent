# Cold Python source evaluation

`check-cold-python-source.py` evaluates the real uv2nix Python environment through
`nix flake check --no-build`, which uses a read-only store. It gives the filtered
build source a fresh name on each run so a previously registered source cannot
hide evaluation-time reads from that source.

Run from a Git checkout with its locked Nix inputs available:

```sh
python3 tests/nix/check-cold-python-source.py
```

The test permits no builds or import-from-derivation. The generated Nix CI runs
it before building the production minimal package and the lifecycle check.
Its Git reference permits shallow checkouts, so hosted CI does not require
full-history `revCount` metadata. Nix diagnostics remain visible on stderr.
The locked Git source and its inputs are archived first: newer Nix versions
can report a source's store path in metadata before materializing it. Only
the original sources are warmed; the UUID-renamed filtered payload stays cold.

`nix/python.nix` reads lock/project metadata from the original workspace and
retains the filtered source for wheel builds. The editable environment shares
the metadata workspace and resolves its payload from the live checkout.

## Hosted fork runners

General CI defaults to standard hosted Linux, Windows x64, Windows ARM64, and
macOS runners. Owners with provisioned larger runners can set repository
variables `HERMES_LINUX_RUNNER`, `HERMES_WINDOWS_RUNNER`, and
`HERMES_WINDOWS_ARM_RUNNER`; `HERMES_TEST_WORKERS` controls full-suite file
parallelism (default two). E2E files run serially to leave CPU for their child
processes. Nix builds use one job and two cores. The complete test selections
and native architecture coverage are retained.
