"""Repo-root conftest: make the add-on package importable for tests.

The add-on lives in ``ergon_usage/`` (its ``app`` package mirrors the
container layout via PYTHONPATH), so tests import ``from app...`` with the
add-on directory on ``sys.path``.
"""

import sys
from pathlib import Path

ADDON_DIR = Path(__file__).parent / "ergon_usage"
sys.path.insert(0, str(ADDON_DIR))
