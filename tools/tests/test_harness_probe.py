"""Paused automation must return without reconciling or writing."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("merge_controller", TOOLS / "merge_controller.py")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
sys.path.pop(0)


class AutomationSwitchTests(unittest.TestCase):
    def test_disabled_automation_returns_without_reconciling_or_writing(self):
        with (
            patch.object(controller.ai, "verify_origin"),
            patch.dict(os.environ, {"UMSAL_AUTO_MERGE": "false"}),
            patch.object(controller.Controller, "reconcile") as reconcile,
        ):
            self.assertEqual(controller.main(["--execute"]), 0)
        reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
