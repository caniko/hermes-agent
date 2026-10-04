"""Emit each failed qualification assertion before later teardown can block."""


def pytest_runtest_logreport(report):
    if report.failed:
        print(f"\nQUALIFICATION_FAILURE {report.nodeid} ({report.when})\n{report.longrepr}", flush=True)
