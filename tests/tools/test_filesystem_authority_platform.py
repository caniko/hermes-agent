"""Unsupported native hosts fail before Linux-only socket APIs are accessed."""

import pytest

from tools.environments.filesystem_authority_server import socket_parent


@pytest.mark.platforms("macos", "windows")
def test_authority_socket_requires_linux_before_accessing_platform_constants(tmp_path):
    with pytest.raises(RuntimeError, match="filesystem authority sockets require Linux"):
        socket_parent(tmp_path / "control")
