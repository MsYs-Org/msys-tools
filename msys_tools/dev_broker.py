"""Local, loopback-only command broker for the Windows MSYS shortcut.

The development CLI normally runs inside WSL because its source tree and SSH
key live there.  Starting ``wsl.exe`` for every small inspection command is
noticeable on Windows, even though SSH itself is already multiplexed.  This
module keeps one *local* WSL Python process alive and accepts a tightly scoped
JSON request over ``127.0.0.1``.  Each request still starts an isolated
``msys_tools.dev`` child process; it never evaluates a shell string.

It is intentionally not a general remote-control service:

* it binds only to loopback;
* the PowerShell launcher generates a random per-broker token;
* the protocol accepts a bounded list of string arguments, not a command line;
* no target credentials, listeners, or files are created on the device.

The module is started and stopped by ``msys.ps1 broker``.  It is useful only
for a local Windows developer session and is not part of MSYS on the target.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 128 * 1024
MAX_ARGUMENTS = 512
MAX_ARGUMENT_BYTES = 16 * 1024
DEFAULT_IDLE_SECONDS = 4 * 60 * 60


class BrokerProtocolError(ValueError):
    """A request is malformed, unauthenticated, or outside the broker ABI."""


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    request_type: str
    argv: tuple[str, ...] = ()


CommandFactory = Callable[[tuple[str, ...]], Sequence[str]]


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )


def _safe_error(error: Exception) -> str:
    """Return a bounded client-facing protocol error without traceback data."""
    message = str(error).replace("\x00", " ").strip()
    return message[:512] or error.__class__.__name__


def _read_token_state(path: str) -> str:
    """Read the user-local PowerShell state file without exposing its token in ps."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read broker token state: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("broker token state must be a JSON object")
    token = payload.get("token")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("broker token state has no valid token")
    return token


class _BrokerHandler(socketserver.StreamRequestHandler):
    """Handle exactly one request per local TCP connection."""

    server: "CommandBroker"

    def send(self, payload: Mapping[str, Any]) -> None:
        self.wfile.write(_json_bytes(payload))
        self.wfile.flush()

    def handle(self) -> None:
        self.server.note_activity()
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_REQUEST_BYTES:
                raise BrokerProtocolError("request exceeds the broker size limit")
            request = self.server.parse_request(raw)
            if request.request_type == "ping":
                self.send(
                    {
                        "type": "ready",
                        "protocol": PROTOCOL_VERSION,
                        "pid": os.getpid(),
                    }
                )
                return
            if request.request_type == "stop":
                self.send({"type": "stopping", "protocol": PROTOCOL_VERSION})
                self.server.stop_async()
                return
            self._run_command(request.argv)
        except BrokerProtocolError as exc:
            self._send_error("protocol", _safe_error(exc))
        except (BrokenPipeError, ConnectionResetError):
            # A caller cancelled (for example Ctrl+C in PowerShell).  There is
            # no useful response route left; the command loop below also kills
            # its local child when this happens.
            return
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_error("internal", _safe_error(exc))

    def _send_error(self, kind: str, message: str) -> None:
        try:
            self.send({"type": "error", "kind": kind, "message": message})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _run_command(self, argv: tuple[str, ...]) -> None:
        command = list(self.server.command_factory(argv))
        if not command or any(not isinstance(item, str) or "\x00" in item for item in command):
            raise BrokerProtocolError("broker command factory returned an invalid command")

        process = subprocess.Popen(
            command,
            cwd=self.server.workspace,
            env=self.server.child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.server.register_process(process)
        disconnected = False
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read1(8192)
                if not chunk:
                    break
                try:
                    self.send(
                        {
                            "type": "output",
                            "data": chunk.decode("utf-8", errors="replace"),
                        }
                    )
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True
                    break
            if disconnected:
                self.server.terminate_process(process)
            exit_code = process.wait()
            if not disconnected:
                self.send({"type": "done", "exit_code": exit_code})
        finally:
            if process.poll() is None:
                self.server.terminate_process(process)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            self.server.unregister_process(process)
            self.server.note_activity()


class CommandBroker(socketserver.ThreadingTCPServer):
    """A token-gated, loopback-only runner for short-lived CLI children."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        token: str,
        workspace: Path,
        child_environment: Mapping[str, str],
        command_factory: CommandFactory,
        idle_seconds: int = DEFAULT_IDLE_SECONDS,
    ) -> None:
        if not token or len(token) < 32:
            raise ValueError("broker token must contain at least 32 characters")
        if idle_seconds < 0:
            raise ValueError("idle_seconds must be non-negative")
        self.token = token
        self.workspace = str(workspace)
        self.child_environment = dict(child_environment)
        self.command_factory = command_factory
        self.idle_seconds = idle_seconds
        self._active_processes: set[subprocess.Popen[bytes]] = set()
        self._process_lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._stopping = threading.Event()
        super().__init__(address, _BrokerHandler, bind_and_activate=True)

    def parse_request(self, raw: bytes) -> BrokerRequest:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerProtocolError("request must be one UTF-8 JSON object") from exc
        if not isinstance(payload, dict):
            raise BrokerProtocolError("request must be a JSON object")
        if payload.get("protocol") != PROTOCOL_VERSION:
            raise BrokerProtocolError(
                f"unsupported broker protocol (expected {PROTOCOL_VERSION})"
            )
        supplied_token = payload.get("token")
        if not isinstance(supplied_token, str) or not secrets.compare_digest(
            supplied_token, self.token
        ):
            raise BrokerProtocolError("broker authentication failed")
        request_type = payload.get("type")
        if request_type in {"ping", "stop"}:
            return BrokerRequest(request_type)
        if request_type != "run":
            raise BrokerProtocolError("unsupported broker request")
        value = payload.get("argv")
        if not isinstance(value, list) or len(value) > MAX_ARGUMENTS:
            raise BrokerProtocolError("argv must be a bounded JSON string array")
        argv: list[str] = []
        for argument in value:
            if not isinstance(argument, str) or "\x00" in argument:
                raise BrokerProtocolError("argv contains an invalid argument")
            if len(argument.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                raise BrokerProtocolError("argv contains an oversized argument")
            argv.append(argument)
        return BrokerRequest("run", tuple(argv))

    def note_activity(self) -> None:
        self._last_activity = time.monotonic()

    def register_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._process_lock:
            self._active_processes.add(process)
        self.note_activity()

    def unregister_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._process_lock:
            self._active_processes.discard(process)

    def has_active_processes(self) -> bool:
        with self._process_lock:
            return bool(self._active_processes)

    def terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            # ``start_new_session`` makes this cover ssh/tail grandchildren on
            # Linux without relying on a shell command line.
            os.killpg(process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError):
            process.terminate()
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                process.kill()

    def stop_async(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()

        def stop() -> None:
            with self._process_lock:
                processes = tuple(self._active_processes)
            for process in processes:
                self.terminate_process(process)
            self.shutdown()

        threading.Thread(target=stop, name="msys-dev-broker-stop", daemon=True).start()

    def serve_until_stopped(self) -> None:
        if self.idle_seconds == 0:
            self.serve_forever(poll_interval=0.25)
            return

        def idle_watchdog() -> None:
            while not self._stopping.wait(1.0):
                if self.has_active_processes():
                    continue
                if time.monotonic() - self._last_activity >= self.idle_seconds:
                    self.stop_async()
                    return

        threading.Thread(
            target=idle_watchdog, name="msys-dev-broker-idle", daemon=True
        ).start()
        self.serve_forever(poll_interval=0.25)


def _default_command_factory(python: str) -> CommandFactory:
    def command(argv: tuple[str, ...]) -> Sequence[str]:
        return (python, "-m", "msys_tools.dev", *argv)

    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="local loopback broker used by MSYS Windows development tooling"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    token = parser.add_mutually_exclusive_group(required=True)
    token.add_argument("--token")
    token.add_argument("--token-state")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--idle-seconds", type=int, default=DEFAULT_IDLE_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host != "127.0.0.1":
        print("msys-dev-broker: only 127.0.0.1 is permitted", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("msys-dev-broker: port must be 1..65535", file=sys.stderr)
        return 2
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"msys-dev-broker: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2

    try:
        token = args.token if args.token is not None else _read_token_state(args.token_state)
    except ValueError as exc:
        print(f"msys-dev-broker: {exc}", file=sys.stderr)
        return 2

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with CommandBroker(
            (args.host, args.port),
            token=token,
            workspace=workspace,
            child_environment=environment,
            command_factory=_default_command_factory(sys.executable),
            idle_seconds=args.idle_seconds,
        ) as server:
            server.serve_until_stopped()
    except (OSError, ValueError) as exc:
        print(f"msys-dev-broker: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI entry point
    raise SystemExit(main())
