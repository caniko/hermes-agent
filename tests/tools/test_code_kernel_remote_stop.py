"""A stopped supervised transfer is interruption only with target settlement proof."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.code_kernel_remote import _run_remote_cell


@pytest.mark.parametrize("alive", [False, True])
def test_failed_cell_transfer_requires_kernel_settlement_for_interruption(monkeypatch, alive):
    ship = Mock(side_effect=RuntimeError("lost cell transfer acknowledgement"))
    monkeypatch.setattr("tools.code_execution_tool._ship_file_to_remote", ship)
    kernel = SimpleNamespace(cell_seq=0, kernel_dir="/owned/kernel", env=object(),
                             supervised_process=object(), is_alive=Mock(return_value=alive))
    if alive:
        with pytest.raises(RuntimeError, match="acknowledgement"):
            _run_remote_cell(kernel, "print('once')", 10)
    else:
        assert _run_remote_cell(kernel, "print('once')", 10) == ("interrupted", {})
    ship.assert_called_once()
