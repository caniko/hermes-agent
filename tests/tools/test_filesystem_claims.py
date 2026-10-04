"""Independent target clients share durable ownership, not controller leases."""

from concurrent.futures import ThreadPoolExecutor

import pytest


def test_overlapping_aliases_queue_atomically_and_survive_restart(tmp_path):
    from tools.environments.filesystem_claims import ClaimStore

    root = tmp_path / "data "
    root.mkdir()
    child = root / "child"
    child.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    other = tmp_path / "other"
    other.mkdir()
    state = tmp_path / "state"
    ClaimStore.initialize(state)
    a, b = ClaimStore(state), ClaimStore(state)
    try:
        def acquire(store, principal, path):
            return store.reserve(principal, "attempt", "fingerprint", [str(path)])
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(acquire, a, "controller-a", alias),
                       pool.submit(acquire, b, "controller-b", child)]
            receipts = [future.result() for future in futures]
        assert sorted(row["state"] for row in receipts) == ["active", "pending"]
        owner = next(row for row in receipts if row["state"] == "active")
        waiter = next(row for row in receipts if row["state"] == "pending")
        assert a.reserve("controller-c", "attempt", "different", [str(other)])["state"] == "active"
        a.close()
        a = ClaimStore(state)
        assert a.get(owner["principal"], "attempt")["state"] == "active"
        assert a.reserve(waiter["principal"], "attempt", "fingerprint", [waiter["roots"][0]["path"]])["state"] == "pending"
        with pytest.raises(ValueError, match="immutable"):
            b.reserve(owner["principal"], "attempt", "changed", [str(other)])
        a.seal(owner["principal"], "attempt", "fingerprint")
        assert b.get(waiter["principal"], "attempt")["state"] == "pending"
        a.settle(owner["id"])
        assert b.reserve(waiter["principal"], "attempt", "fingerprint", [waiter["roots"][0]["path"]])["state"] == "active"
    finally:
        a.close()
        b.close()


def test_stop_before_create_and_changed_root_cannot_resurrect_ownership(tmp_path):
    from tools.environments.filesystem_claims import ClaimStore

    state = tmp_path / "state"
    ClaimStore.initialize(state)
    store = ClaimStore(state)
    root = tmp_path / "data"
    root.mkdir()
    try:
        stopped = store.seal("controller", "late", "fingerprint", stop=True)
        assert stopped["state"] == "settled"
        assert store.reserve("controller", "late", "fingerprint", [str(root)])["state"] == "settled"
        owner = store.reserve("controller", "live", "fingerprint", [str(root)])
        root.rename(tmp_path / "renamed")
        root.mkdir()
        with pytest.raises(ValueError, match="identity"):
            store.validate_roots(owner["id"])
        # Neither a changed pathname nor a renamed inode permits a second owner.
        assert store.reserve("other", "new", "fp", [str(root)])["state"] == "pending"
        assert store.reserve("other", "renamed", "fp", [str(tmp_path / "renamed")])["state"] == "pending"
        with pytest.raises(ValueError, match="sealed"):
            store.settle(owner["id"])
    finally:
        store.close()


def test_conservative_domains_serialize_siblings_without_expanding_immutable_write_grants(tmp_path):
    from tools.environments.filesystem_claims import ClaimStore

    domain = tmp_path / "data"
    domain.mkdir()
    children = [domain / "alice", domain / "bob"]
    for child in children:
        child.mkdir()
    state = tmp_path / "state"
    ClaimStore.initialize(state)
    store = ClaimStore(state)
    try:
        first = store.reserve("a", "one", "fp", [str(children[0])], domains=[str(domain)])
        assert [root["path"] for root in first["roots"]] == [str(children[0])]
        assert store.reserve("b", "two", "fp", [str(children[1])], domains=[str(domain)])["state"] == "pending"
        with pytest.raises(ValueError, match="immutable"):
            store.reserve("a", "one", "fp", [str(children[1])], domains=[str(domain)])
        store.seal("a", "one", "fp")
        store.settle(first["id"])
        assert store.reserve("b", "two", "fp", [str(children[1])], domains=[str(domain)])["state"] == "active"
    finally:
        store.close()


@pytest.mark.parametrize("change", ["move", "alias"])
def test_requested_inode_and_alias_stay_reserved_when_they_leave_the_enrolled_domain(tmp_path, change):
    from tools.environments.filesystem_claims import ClaimStore

    domains = [tmp_path / "first", tmp_path / "second"]
    for domain in domains:
        domain.mkdir()
    root = domains[0] / "project"
    root.mkdir()
    alias = tmp_path / "selected-directory"
    alias.symlink_to(root, target_is_directory=True)
    state = tmp_path / "state"
    ClaimStore.initialize(state)
    store = ClaimStore(state)
    try:
        store.reserve("a", "one", "fp", [str(alias)], domains=[str(domains[0])])
        if change == "move":
            replacement = domains[1] / "moved-project"
            root.rename(replacement)
            selected = replacement
        else:
            replacement = domains[1] / "replacement"
            replacement.mkdir()
            alias.unlink()
            alias.symlink_to(replacement, target_is_directory=True)
            selected = alias
        # A new domain must not free either the live inode or the reserved name.
        assert store.reserve("b", "two", "fp", [str(selected)], domains=[str(domains[1])])["state"] == "pending"
        store.seal("a", "one", "fp")
        store.settle(store.get("a", "one")["id"])
        assert store.reserve("b", "two", "fp", [str(selected)], domains=[str(domains[1])])["state"] == "active"
    finally:
        store.close()
