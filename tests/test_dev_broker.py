from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from msys_tools.dev_broker import CommandBroker, PROTOCOL_VERSION, _read_token_state


class CommandBrokerTests(unittest.TestCase):
    token = "a" * 64

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

        def command_factory(argv: tuple[str, ...]) -> list[str]:
            return [
                sys.executable,
                "-c",
                (
                    "import json, sys; "
                    "print(json.dumps(sys.argv[1:], ensure_ascii=False)); "
                    "print('stderr-from-child', file=sys.stderr)"
                ),
                *argv,
            ]

        self.server = CommandBroker(
            ("127.0.0.1", 0),
            token=self.token,
            workspace=self.workspace,
            child_environment={},
            command_factory=command_factory,
            idle_seconds=0,
        )
        self.thread = threading.Thread(
            target=self.server.serve_until_stopped, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.stop_async()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, payload: dict[str, object]) -> list[dict[str, object]]:
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            stream = client.makefile("rb")
            return [json.loads(line) for line in stream if line]

    def test_run_streams_stdout_and_stderr_without_a_shell(self) -> None:
        frames = self.request(
            {
                "protocol": PROTOCOL_VERSION,
                "token": self.token,
                "type": "run",
                "argv": ["one;not-a-shell", "中文"],
            }
        )

        self.assertEqual(frames[-1], {"type": "done", "exit_code": 0})
        output = "".join(
            str(frame["data"])
            for frame in frames
            if frame.get("type") == "output"
        )
        self.assertIn('["one;not-a-shell", "中文"]', output)
        self.assertIn("stderr-from-child", output)

    def test_rejects_an_invalid_token_before_starting_a_child(self) -> None:
        frames = self.request(
            {
                "protocol": PROTOCOL_VERSION,
                "token": "b" * 64,
                "type": "run",
                "argv": ["config", "show"],
            }
        )

        self.assertEqual(frames[0]["type"], "error")
        self.assertEqual(frames[0]["kind"], "protocol")
        self.assertIn("authentication", str(frames[0]["message"]))

    def test_rejects_nul_arguments(self) -> None:
        frames = self.request(
            {
                "protocol": PROTOCOL_VERSION,
                "token": self.token,
                "type": "run",
                "argv": ["bad\x00argument"],
            }
        )

        self.assertEqual(frames[0]["type"], "error")
        self.assertIn("invalid argument", str(frames[0]["message"]))

    def test_ping_reports_the_protocol_without_starting_a_child(self) -> None:
        frames = self.request(
            {
                "protocol": PROTOCOL_VERSION,
                "token": self.token,
                "type": "ping",
            }
        )

        self.assertEqual(frames[0]["type"], "ready")
        self.assertEqual(frames[0]["protocol"], PROTOCOL_VERSION)
        self.assertIsInstance(frames[0]["pid"], int)

    def test_token_can_be_loaded_from_the_user_local_state_document(self) -> None:
        state = self.workspace / "broker.json"
        state.write_text(json.dumps({"token": self.token}), encoding="utf-8")

        self.assertEqual(_read_token_state(str(state)), self.token)

    def test_token_state_rejects_missing_or_short_secrets(self) -> None:
        state = self.workspace / "broker.json"
        state.write_text('{"token":"short"}', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "valid token"):
            _read_token_state(str(state))


if __name__ == "__main__":
    unittest.main()
