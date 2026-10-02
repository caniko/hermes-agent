"""Fixture SSH client that imports the checkout without a remote installation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.environments.filesystem_authority_server import main

main()
