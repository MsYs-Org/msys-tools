from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .remote_ctl import call


CONTROL_SCHEMA = "msys.shield-control.v1"
STATUS_SCHEMA = "msys.screen-shield.status.v1"
SCREEN_SHIELD_ROLE = "screen-shield"


class ShieldControlError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ShieldControlError(
            "SHIELD_TIMEOUT",
            "screen-shield operation exceeded its deadline",
        )
    return remaining


def _rpc_payload(
    runtime_dir: str,
    target: str,
    method: str,
    payload: dict[str, Any],
    deadline: float,
    *,
    idempotent: bool,
) -> dict[str, Any]:
    result = call(
        runtime_dir,
        target,
        method,
        payload,
        timeout=_remaining(deadline),
        idempotent=idempotent,
    )
    welcome = result.get("welcome") if isinstance(result, dict) else None
    if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
        raise ShieldControlError(
            "MALFORMED_WELCOME",
            f"{target}.{method} did not receive a valid Core welcome",
        )
    response = result.get("response")
    if not isinstance(response, dict):
        raise ShieldControlError(
            "MALFORMED_RESPONSE",
            f"{target}.{method} returned a non-object response",
        )
    if response.get("type") != "return":
        code = str(response.get("code") or "RPC_FAILED")
        message = str(response.get("message") or f"{target}.{method} failed")
        raise ShieldControlError(
            code,
            message[:512],
            details={
                "target": target,
                "method": method,
                "response": response,
            },
        )
    returned = response.get("payload")
    if not isinstance(returned, dict):
        raise ShieldControlError(
            "MALFORMED_RESPONSE",
            f"{target}.{method} returned a non-object payload",
        )
    return returned


def _screen_shield_role(runtime_dir: str, deadline: float) -> dict[str, Any]:
    payload = _rpc_payload(
        runtime_dir,
        "msys.core",
        "list_roles",
        {},
        deadline,
        idempotent=True,
    )
    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, list):
        raise ShieldControlError(
            "MALFORMED_ROLE_CATALOG",
            "Core list_roles returned a non-array roles field",
        )
    matches = [
        item
        for item in raw_roles
        if isinstance(item, dict) and item.get("role") == SCREEN_SHIELD_ROLE
    ]
    if not matches:
        raise ShieldControlError(
            "UNKNOWN_ROLE",
            "the screen-shield role is not installed in the active catalog",
        )
    if len(matches) != 1:
        raise ShieldControlError(
            "MALFORMED_ROLE_CATALOG",
            "Core returned duplicate screen-shield role records",
        )

    role = matches[0]
    raw_candidates = role.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ShieldControlError(
            "MALFORMED_ROLE_CATALOG",
            "screen-shield candidates is not an array",
        )
    candidates: dict[str, dict[str, Any]] = {}
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            raise ShieldControlError(
                "MALFORMED_ROLE_CATALOG",
                "screen-shield contains a non-object candidate",
            )
        component = candidate.get("component")
        state = candidate.get("state")
        if (
            not isinstance(component, str)
            or not component
            or not isinstance(state, str)
            or not state
            or component in candidates
        ):
            raise ShieldControlError(
                "MALFORMED_ROLE_CATALOG",
                "screen-shield contains an invalid or duplicate candidate",
            )
        candidates[component] = candidate

    for field in ("active", "preferred"):
        provider = role.get(field)
        if provider is not None and (
            not isinstance(provider, str)
            or not provider
            or provider not in candidates
        ):
            raise ShieldControlError(
                "MALFORMED_ROLE_CATALOG",
                f"screen-shield {field} provider is not a catalog candidate",
            )
    return role


def _validate_status(payload: dict[str, Any], *, visible: bool) -> dict[str, Any]:
    if payload.get("schema") != STATUS_SCHEMA:
        raise ShieldControlError(
            "BAD_SHIELD_STATUS",
            f"unexpected screen-shield status schema {payload.get('schema')!r}",
        )
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ShieldControlError(
            "BAD_SHIELD_STATUS",
            "screen-shield returned an invalid revision",
        )
    if payload.get("visible") is not visible:
        raise ShieldControlError(
            "SHIELD_STATE_MISMATCH",
            f"screen-shield did not become {'visible' if visible else 'hidden'}",
            details={"status": payload},
        )
    if not isinstance(payload.get("touch_dismiss_enabled"), bool):
        raise ShieldControlError(
            "BAD_SHIELD_STATUS",
            "screen-shield returned an invalid touch-dismiss policy",
        )
    if not isinstance(payload.get("last_reason"), str):
        raise ShieldControlError(
            "BAD_SHIELD_STATUS",
            "screen-shield returned an invalid last reason",
        )
    changed = payload.get("changed")
    if changed is not None and not isinstance(changed, bool):
        raise ShieldControlError(
            "BAD_SHIELD_STATUS",
            "screen-shield returned an invalid changed flag",
        )
    return payload


def control_shield(
    runtime_dir: str,
    action: str,
    *,
    timeout: float = 45.0,
) -> dict[str, Any]:
    if action not in {"show", "hide"}:
        raise ValueError(f"unsupported shield action: {action}")
    deadline = time.monotonic() + timeout
    role = _screen_shield_role(runtime_dir, deadline)
    active = role.get("active")
    preferred = role.get("preferred")

    if action == "hide" and active is None:
        # A provider starts with visible=false. Calling the role here would
        # make Core start an on-demand/manual provider merely to hide it, so a
        # missing active lease is the authoritative idempotent no-op case.
        return {
            "schema": CONTROL_SCHEMA,
            "action": action,
            "ok": True,
            "role": SCREEN_SHIELD_ROLE,
            "provider": preferred,
            "provider_running": False,
            "changed": False,
            "already_hidden": True,
            "reason": "provider-not-running",
        }

    provider = active or preferred
    if not isinstance(provider, str) or not provider:
        raise ShieldControlError(
            "NO_PROVIDER",
            "the screen-shield role has no selected provider",
        )

    if action == "show":
        start = _rpc_payload(
            runtime_dir,
            "msys.core",
            "start",
            {"component": provider},
            deadline,
            idempotent=True,
        )
        if start.get("component") != provider or start.get("state") != "ready":
            raise ShieldControlError(
                "PROVIDER_NOT_READY",
                "Core did not confirm the selected screen-shield provider as ready",
                details={"provider": provider, "start": start},
            )
    status = _validate_status(
        _rpc_payload(
            runtime_dir,
            f"role:{SCREEN_SHIELD_ROLE}",
            action,
            {},
            deadline,
            idempotent=True,
        ),
        visible=action == "show",
    )
    return {
        "schema": CONTROL_SCHEMA,
        "action": action,
        "ok": True,
        "role": SCREEN_SHIELD_ROLE,
        "provider": provider,
        "provider_running": True,
        # A successful typed role reply can only come from an instance that
        # Core made ready, even if the catalog snapshot preceded handshaking.
        "provider_ready": True,
        "changed": status.get("changed"),
        "already_hidden": action == "hide" and status.get("changed") is False,
        "status": status,
    }


def _timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not 0 < timeout <= 600:
        raise argparse.ArgumentTypeError(
            "timeout must be greater than zero and at most 600 seconds"
        )
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m msys_tools.remote_shield")
    parser.add_argument("action", choices=["show", "hide"])
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--timeout", type=_timeout, default=45.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = Path(args.runtime_dir)
    if not runtime.is_absolute() or runtime == Path("/") or ".." in runtime.parts:
        print("runtime directory must be a non-root absolute path without '..'", file=sys.stderr)
        return 2
    try:
        result = control_shield(str(runtime), args.action, timeout=args.timeout)
        status = 0
    except ShieldControlError as exc:
        result = {
            "schema": CONTROL_SCHEMA,
            "action": args.action,
            "ok": False,
            "role": SCREEN_SHIELD_ROLE,
            "error": {
                "code": exc.code,
                "message": exc.message,
                **({"details": exc.details} if exc.details else {}),
            },
        }
        status = 1
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema": CONTROL_SCHEMA,
            "action": args.action,
            "ok": False,
            "role": SCREEN_SHIELD_ROLE,
            "error": {
                "code": "SHIELD_CONTROL_FAILED",
                "message": str(exc)[:512],
            },
        }
        status = 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
