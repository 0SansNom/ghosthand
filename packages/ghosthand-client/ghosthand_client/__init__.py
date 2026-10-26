"""Zero-dependency client for the ghosthand console (`ghosthand ui`).

    from ghosthand_client import GhosthandClient

    client = GhosthandClient("http://localhost:8000")
    result = client.run("Turn on dark mode in Settings", mode="reliable")
    assert result.succeeded, result.summary

The heavy parts (Xcode, the runner, the model) stay on the Mac running the console; tests only need
this module.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = ["GhosthandClient", "GhosthandError", "TaskResult"]

FINISHED = {"success", "failed", "max_steps", "error", "cancelled"}


class GhosthandError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class TaskResult:
    run_id: str
    status: str
    summary: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @property
    def trace_url(self) -> str:
        return f"/api/traces/{self.run_id}"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "TaskResult":
        return cls(run_id=data["runId"], status=data["status"], summary=data.get("summary") or "",
                   steps=data.get("steps", []), plan=data.get("plan", []), raw=data)


class GhosthandClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None, raw: bool = False) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")  # the console refuses other writes
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error", error.reason)
            except ValueError:
                message = error.reason
            raise GhosthandError(str(message), error.code) from None
        except urllib.error.URLError as error:
            raise GhosthandError(f"console unreachable at {self.base_url}: {error.reason} "
                               "(is `ghosthand ui` running?)") from None
        return payload if raw else json.loads(payload)

    def devices(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/devices")

    def start_runner(self, device: str) -> None:
        self._request("POST", f"/api/devices/{_quote(device)}/runner", {"action": "start"})

    def screenshot(self, device: str) -> bytes:
        """JPEG of the current screen, one pixel per point."""
        return self._request("GET", f"/api/devices/{_quote(device)}/screen.jpg", raw=True)

    def elements(self, device: str) -> dict[str, Any]:
        """Foreground app, lock state and the numbered element list (as text and as data)."""
        return self._request("GET", f"/api/devices/{_quote(device)}/elements")

    def tap(self, device: str, x: float, y: float) -> None:
        self._request("POST", f"/api/devices/{_quote(device)}/action", {"type": "tap", "x": x, "y": y})

    def launch(self, device: str, bundle_id: str, relaunch: bool = False) -> None:
        self._request("POST", f"/api/devices/{_quote(device)}/action",
                      {"type": "launch", "bundleId": bundle_id, "relaunch": relaunch})

    def start(self, task: str, device: str | None = None, mode: str = "fast", max_steps: int = 30) -> str:
        body = {"task": task, "device": device, "mode": mode, "maxSteps": max_steps}
        return self._request("POST", "/api/tasks", body)["runId"]

    def status(self, run_id: str) -> TaskResult:
        return TaskResult.from_api(self._request("GET", f"/api/tasks/{run_id}"))

    def cancel(self, run_id: str) -> None:
        self._request("POST", f"/api/tasks/{run_id}/cancel", {})

    def run(self, task: str, device: str | None = None, mode: str = "fast", max_steps: int = 30,
            timeout: float = 600.0, poll: float = 1.0) -> TaskResult:
        run_id = self.start(task, device=device, mode=mode, max_steps=max_steps)
        deadline = time.monotonic() + timeout
        while True:
            result = self.status(run_id)
            if result.status in FINISHED:
                return result
            if time.monotonic() > deadline:
                self.cancel(run_id)
                raise GhosthandError(f"task {run_id} did not finish within {timeout:.0f}s")
            time.sleep(poll)


def _quote(value: str) -> str:
    return urllib.request.quote(value, safe="")
