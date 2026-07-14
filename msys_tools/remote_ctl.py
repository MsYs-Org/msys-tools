from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path


MAX_PACKET = 256 * 1024
DISPLAY_MIGRATION_SCHEMA = "msys.display-migration.v1"
DISPLAY_MIGRATION_WAIT_SCHEMA = "msys.display-migration-wait.v1"
DISPLAY_MIGRATION_PENDING_PHASES = frozenset({"planned", "switching"})
DISPLAY_MIGRATION_TERMINAL_PHASES = frozenset({"succeeded", "rolled-back"})


def send(sock: socket.socket, message: dict) -> None:
    sock.sendall(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")


def recv(sock: socket.socket) -> dict:
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(MAX_PACKET)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_PACKET:
            raise RuntimeError("response too large")
    if not data:
        raise RuntimeError("empty response")
    return json.loads(data.decode("utf-8"))


def call(
    runtime_dir: str,
    target: str,
    method: str,
    payload: dict,
    timeout: float = 30.0,
    *,
    idempotent: bool = False,
) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout + 2.0)
    try:
        sock.connect(str(Path(runtime_dir) / "control.sock"))
        welcome = recv(sock)
        send(sock, {
            "type": "call",
            "id": 1,
            "target": target,
            "method": method,
            "payload": payload,
            "deadline_ms": int(time.monotonic() * 1000 + timeout * 1000),
            "idempotent": bool(idempotent),
        })
        response = recv(sock)
    finally:
        sock.close()
    return {"welcome": welcome, "response": response}


def wait_display_migration(
    runtime_dir: str,
    migration_id: int,
    timeout: float,
    *,
    poll_interval: float = 0.2,
) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        result = call(
            runtime_dir,
            "msys.core",
            "display_migration_status",
            {"id": migration_id},
            timeout=min(5.0, remaining),
            idempotent=True,
        )
        response = result.get("response", {})
        if response.get("type") != "return":
            return {
                "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                "ok": False,
                "migration_id": migration_id,
                "response": response,
            }
        payload = response.get("payload")
        migration = payload.get("migration") if isinstance(payload, dict) else None
        if not isinstance(migration, dict):
            return {
                "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                "ok": False,
                "migration_id": migration_id,
                "error": "Core returned a malformed display migration status",
            }
        if migration.get("schema") != DISPLAY_MIGRATION_SCHEMA:
            return {
                "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                "ok": False,
                "migration_id": migration_id,
                "migration": migration,
                "error": (
                    "Core returned an unexpected display migration schema: "
                    f"{migration.get('schema')!r}"
                ),
            }
        record_id = migration.get("id")
        if (
            isinstance(record_id, bool)
            or not isinstance(record_id, int)
            or record_id != migration_id
        ):
            return {
                "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                "ok": False,
                "migration_id": migration_id,
                "migration": migration,
                "error": (
                    "Core returned display migration id "
                    f"{record_id!r}, expected {migration_id}"
                ),
            }
        latest = migration
        phase = migration.get("phase")
        if phase in DISPLAY_MIGRATION_TERMINAL_PHASES:
            return {
                "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                "ok": phase == "succeeded",
                "migration_id": migration_id,
                "migration": migration,
            }
        if phase not in DISPLAY_MIGRATION_PENDING_PHASES:
            return {
                "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                "ok": False,
                "migration_id": migration_id,
                "migration": migration,
                "error": f"unexpected migration phase: {phase}",
            }
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
    return {
        "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
        "ok": False,
        "migration_id": migration_id,
        "migration": latest,
        "error": f"display migration did not finish within {timeout:g} seconds",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--target", default="msys.core")
    parser.add_argument("--method", required=True)
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--response-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--idempotent", action="store_true")
    parser.add_argument("--wait-display-migration", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = call(
            args.runtime_dir,
            args.target,
            args.method,
            json.loads(args.payload),
            timeout=args.timeout,
            idempotent=args.idempotent,
        )
        response = result.get("response", {})
        status = 0 if response.get("type") == "return" else 1
        if args.wait_display_migration and status == 0:
            migration = response.get("payload", {}).get("migration")
            migration_id = migration.get("id") if isinstance(migration, dict) else None
            if isinstance(migration_id, bool) or not isinstance(migration_id, int) or migration_id <= 0:
                result["migration_wait"] = {
                    "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                    "ok": False,
                    "error": "role response did not contain a valid migration id",
                }
                status = 1
            elif migration.get("schema") != DISPLAY_MIGRATION_SCHEMA:
                result["migration_wait"] = {
                    "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                    "ok": False,
                    "migration_id": migration_id,
                    "migration": migration,
                    "error": (
                        "role response contained an unexpected display migration "
                        f"schema: {migration.get('schema')!r}"
                    ),
                }
                status = 1
            elif migration.get("phase") not in (
                DISPLAY_MIGRATION_PENDING_PHASES
                | DISPLAY_MIGRATION_TERMINAL_PHASES
            ):
                result["migration_wait"] = {
                    "schema": DISPLAY_MIGRATION_WAIT_SCHEMA,
                    "ok": False,
                    "migration_id": migration_id,
                    "migration": migration,
                    "error": (
                        "role response contained an unexpected display migration "
                        f"phase: {migration.get('phase')!r}"
                    ),
                }
                status = 1
            else:
                waited = wait_display_migration(
                    args.runtime_dir,
                    migration_id,
                    args.timeout,
                )
                result["migration_wait"] = waited
                status = 0 if waited.get("ok") is True else 1
        print(json.dumps(result["response"] if args.response_only else result, indent=2))
        return status
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "type": "error",
            "code": "REMOTE_CONTROL_FAILED",
            "message": str(exc),
        }, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
