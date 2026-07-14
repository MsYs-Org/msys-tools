from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from msys_tools.dev import Context, command_activate


class ActivateCommandTests(unittest.TestCase):
    def test_uri_infers_open_uri_action(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        with mock.patch("msys_tools.dev.remote_control_command", return_value=0) as call:
            result = command_activate(
                context,
                "/tmp/msys-main",
                action=None,
                uri="demo://item",
                mime=None,
                name=None,
                component=None,
            )
        self.assertEqual(result, 0)
        self.assertEqual(call.call_args.args[3]["action"], "open-uri")


if __name__ == "__main__":
    unittest.main()
