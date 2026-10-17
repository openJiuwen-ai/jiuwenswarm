"""Keep the Agent's declared API stable when internal code is reorganized."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import computer_use
from computer_use import DragOptions, FindOptions, ReadOptions
from computer_use.scripts.native import permissions


class PublicContractTests(unittest.TestCase):
    def test_option_keys_match_the_existing_agent_contract(self):
        self.assertEqual(DragOptions.__required_keys__, {"fromX", "fromY", "toX", "toY"})
        self.assertEqual(ReadOptions.__optional_keys__, {"full", "menuPath"})
        self.assertEqual(FindOptions.__optional_keys__, {"timeoutMs"})

    def test_declared_public_exports_exist(self):
        for name in computer_use.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(computer_use, name))

    def test_permissions_stdout_stays_machine_readable_when_diagnostics_are_logged(self):
        status = {"accessibility": False, "screenCapture": True, "input": True}
        output = io.StringIO()
        with patch("computer_use.scripts.native.read_permissions", return_value=status):
            with redirect_stdout(output), self.assertLogs("computer_use.scripts.native", level="ERROR") as logs:
                result = permissions([])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), status)
        self.assertIn("accessibility", logs.output[0])
