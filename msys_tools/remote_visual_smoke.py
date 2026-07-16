from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from .remote_ctl import call


VISUAL_SMOKE_SCHEMA = "msys.visual-smoke.v1"
DEFAULT_VISUAL_SMOKE_COMPONENT = "org.msys.calculator:calculator"


class VisualSmokeError(RuntimeError):
    """A typed visual smoke-test step failed."""


RpcCallable = Callable[..., dict[str, Any]]


def _component_id(value: str) -> str:
    component = value.strip()
    if (
        not component
        or ":" not in component
        or len(component) > 255
        or any(ord(character) < 33 or ord(character) > 126 for character in component)
    ):
        raise VisualSmokeError(
            "component must be a package:component identifier of 1-255 printable characters"
        )
    return component


def _payload(response: object, step: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise VisualSmokeError(f"{step} returned a non-object response")
    if response.get("type") != "return":
        code = response.get("code") or "REMOTE_ERROR"
        message = response.get("message") or "typed call failed"
        raise VisualSmokeError(f"{step} failed: {code}: {message}")
    payload = response.get("payload", {})
    if not isinstance(payload, dict):
        raise VisualSmokeError(f"{step} returned a non-object payload")
    return dict(payload)


def run_visual_smoke(
    runtime_dir: str,
    component: str,
    *,
    timeout: float = 12.0,
    rpc_call: RpcCallable | None = None,
) -> tuple[int, dict[str, Any]]:
    """Exercise Home/start/Back/Recents through typed RPC and restore Home."""

    caller = rpc_call or call
    requested_component = _component_id(component)
    component = requested_component
    steps: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []
    mutation_started = False
    start_attempted = False
    closed_confirmed = False
    error: str | None = None

    def invoke(
        target: str,
        method: str,
        payload: dict[str, Any],
        step: str,
        *,
        cleanup_step: bool = False,
        record_payload: bool = True,
    ) -> dict[str, Any]:
        result = caller(
            runtime_dir,
            target,
            method,
            payload,
            timeout=timeout,
            idempotent=method in {"list_components", "foreground_stack", "recents"},
        )
        response = result.get("response") if isinstance(result, dict) else None
        recorded_response = response
        if not record_payload and isinstance(response, dict):
            recorded_response = {
                key: response[key]
                for key in ("type", "id", "code", "message")
                if key in response
            }
        record = {
            "step": step,
            "target": target,
            "method": method,
            "response": recorded_response,
        }
        (cleanup if cleanup_step else steps).append(record)
        return _payload(response, step)

    try:
        inventory = invoke(
            "msys.core",
            "list_components",
            {},
            "preflight.component",
            record_payload=False,
        )
        raw_components = inventory.get("components", [])
        if not isinstance(raw_components, list):
            raise VisualSmokeError("preflight.component returned an invalid component list")
        descriptor = next(
            (
                dict(item)
                for item in raw_components
                if isinstance(item, dict) and item.get("id") == component
            ),
            None,
        )
        if descriptor is None:
            raise VisualSmokeError(
                f"test component is not declared: {requested_component}"
            )
        if descriptor.get("launchable") is not True or descriptor.get("lifecycle") != "manual":
            raise VisualSmokeError("test component must be a launchable manual application")
        if descriptor.get("state") not in {"declared", "stopped"}:
            raise VisualSmokeError(
                f"test component must be stopped before smoke test (state={descriptor.get('state')})"
            )
        steps[-1]["component"] = {
            key: descriptor.get(key)
            for key in ("id", "lifecycle", "state", "launchable")
        }

        foreground = invoke(
            "msys.core", "foreground_stack", {}, "preflight.foreground"
        ).get("windows", [])
        if not isinstance(foreground, list):
            raise VisualSmokeError("preflight.foreground returned an invalid window list")
        recents = invoke(
            "role:window-manager", "recents", {}, "preflight.recents"
        ).get("windows", [])
        if not isinstance(recents, list):
            raise VisualSmokeError("preflight.recents returned an invalid window list")
        if foreground or recents:
            raise VisualSmokeError(
                "visual-smoke requires a clean Home session with no managed or external user windows"
            )

        mutation_started = True
        home = invoke("role:window-manager", "home", {}, "home")
        if home.get("ok") is False:
            raise VisualSmokeError(
                f"home was rejected: {home.get('reason') or home.get('error') or 'unknown error'}"
            )

        # A timeout can race with a successful start, so cleanup becomes
        # mandatory before opening the request rather than after its reply.
        start_attempted = True
        started = invoke(
            "msys.core", "start", {"component": component}, "start-app"
        )
        if started.get("state") != "ready":
            raise VisualSmokeError(f"application did not become ready: {started.get('state')}")
        if isinstance(started.get("activation_error"), dict):
            details = started["activation_error"]
            raise VisualSmokeError(
                "application window activation failed: "
                f"{details.get('code') or details.get('message') or details}"
            )
        activation = started.get("activation")
        if isinstance(activation, dict) and activation.get("ok") is False:
            raise VisualSmokeError(
                f"application window activation was rejected: {activation.get('reason') or activation.get('stderr')}"
            )

        active_windows = invoke(
            "role:window-manager", "recents", {}, "verify-app-visible"
        ).get("windows", [])
        if not isinstance(active_windows, list) or not any(
            isinstance(item, dict) and item.get("component") == component
            for item in active_windows
        ):
            raise VisualSmokeError("started component did not appear in typed Recents")

        backed = invoke("role:window-manager", "back", {}, "back")
        if backed.get("ok") is False:
            raise VisualSmokeError(
                f"Back was rejected: {backed.get('reason') or backed.get('error') or 'unknown error'}"
            )
        closed_confirmed = backed.get("closed_component") == component
        if not closed_confirmed:
            raise VisualSmokeError(
                f"Back did not close the test component (closed={backed.get('closed_component')!r})"
            )

        after = invoke(
            "role:window-manager", "recents", {}, "recents-after-back"
        ).get("windows", [])
        if not isinstance(after, list):
            raise VisualSmokeError("recents-after-back returned an invalid window list")
        if any(
            isinstance(item, dict) and item.get("component") == component
            for item in after
        ):
            closed_confirmed = False
            raise VisualSmokeError("test component remained in Recents after Back")
    except (OSError, RuntimeError, ValueError) as exc:
        error = str(exc)
    finally:
        if start_attempted and not closed_confirmed:
            try:
                invoke(
                    "msys.core",
                    "stop",
                    {"component": component},
                    "cleanup.stop-test-component",
                    cleanup_step=True,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                cleanup.append({
                    "step": "cleanup.stop-test-component",
                    "error": str(exc),
                })
                error = error or f"cleanup failed: {exc}"
        if mutation_started:
            try:
                restored = invoke(
                    "role:window-manager",
                    "home",
                    {},
                    "cleanup.restore-home",
                    cleanup_step=True,
                )
                if restored.get("ok") is False:
                    raise VisualSmokeError(
                        restored.get("reason") or restored.get("error") or "Home restore was rejected"
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                cleanup.append({"step": "cleanup.restore-home", "error": str(exc)})
                error = error or f"cleanup failed: {exc}"

    document = {
        "schema": VISUAL_SMOKE_SCHEMA,
        "ok": error is None,
        "component": component,
        "requested_component": requested_component,
        "steps": steps,
        "cleanup": cleanup,
        "restored": not mutation_started or not any("error" in item for item in cleanup),
    }
    if error is not None:
        document["error"] = error
    return (0 if error is None else 1), document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="run a reversible typed Home/start/Back/Recents visual smoke test"
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--timeout", type=float, default=12.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (0 < args.timeout <= 120):
        print(json.dumps({
            "schema": VISUAL_SMOKE_SCHEMA,
            "ok": False,
            "error": "timeout must be greater than zero and at most 120 seconds",
        }, indent=2))
        return 2
    try:
        status, document = run_visual_smoke(
            args.runtime_dir,
            args.component,
            timeout=args.timeout,
        )
    except VisualSmokeError as exc:
        status = 2
        document = {
            "schema": VISUAL_SMOKE_SCHEMA,
            "ok": False,
            "error": str(exc),
            "steps": [],
            "cleanup": [],
            "restored": True,
        }
    print(json.dumps(document, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
