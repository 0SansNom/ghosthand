import json
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import runner
from .device import Device
from .errors import GhosthandError
from .observe import prepare
from .paths import TRACES_DIR
from .planning import plan_to_dicts
from .targets import list_targets, resolve_target
from .tasks import Task, TaskManager
from .tree import render

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
RUN_ID = re.compile(r"^[\w-]+$")
TRACE_FILE = re.compile(r"^step-\d{3}\.jpg$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


class LocalOnly:
    # This API can tap on a real phone: refuse other hosts (DNS rebinding) and cross-site writes (CSRF).
    # A cross-site page can't send `Content-Type: application/json` without a CORS preflight, which is
    # never approved, so requiring it on writes keeps other sites out.

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            host = headers.get("host", "")
            hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            problem = None
            if hostname not in LOCAL_HOSTS:
                problem = "only local requests are accepted"
            elif scope["method"] not in ("GET", "HEAD"):
                origin = headers.get("origin")
                if not headers.get("content-type", "").startswith("application/json"):
                    problem = "writes must be JSON"
                elif origin and origin not in (f"http://{host}", f"https://{host}"):
                    problem = "cross-origin writes are refused"
            if problem:
                await JSONResponse({"error": problem}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class Console:
    def __init__(self):
        self.tasks = TaskManager()
        self._devices: dict[str, Device] = {}
        self._lock = threading.Lock()

    def device(self, udid: str, start: bool = False) -> Device:
        # Cached: resolving targets runs devicectl, far too slow for live screen polling.
        with self._lock:
            cached = self._devices.get(udid)
        if cached and (not start or cached.client.is_up()):
            return cached
        try:
            device = Device.connect(udid, start_runner=start)
        except GhosthandError as error:
            raise ApiError(409, str(error)) from error
        with self._lock:
            self._devices[udid] = device
        return device

    def forget(self, udid: str) -> None:
        with self._lock:
            self._devices.pop(udid, None)

    def devices(self) -> list[dict]:
        result = []
        for target in list_targets():
            state = runner.load_state(target.udid)
            result.append({"udid": target.udid, "name": target.name, "kind": target.kind,
                           "runtime": target.runtime, "ready": target.booted, "runner": state is not None})
        return sorted(result, key=lambda d: (not d["ready"], d["kind"], d["name"]))

    def screen_jpeg(self, udid: str) -> bytes:
        device = self.device(udid)
        screen = device.client.screen()
        return prepare(device.client.screenshot(fast=True), screen["width"], screen["height"], quality=70)

    def elements(self, udid: str) -> dict:
        device = self.device(udid)
        health = device.client.health()
        elements = device.elements()
        return {"app": health["currentApp"], "locked": health.get("locked", False), "text": render(elements),
                "elements": [{**asdict(e), "center": e.center} for e in elements]}

    def action(self, udid: str, body: dict[str, Any]) -> dict:
        client = self.device(udid).client
        kind = body.get("type")
        try:
            match kind:
                case "tap":
                    client.tap(float(body["x"]), float(body["y"]), body.get("duration"))
                case "swipe":
                    client.swipe(float(body["fromX"]), float(body["fromY"]), float(body["toX"]), float(body["toY"]),
                                 float(body.get("duration", 0.3)))
                case "type":
                    client.type_text(str(body["text"]))
                case "press":
                    client.press(str(body["button"]))
                case "launch":
                    client.launch(str(body["bundleId"]), bool(body.get("relaunch", False)))
                case "open_url":
                    client.open_url(str(body["url"]))
                case _:
                    raise ApiError(400, f"unknown action {kind!r}")
        except (KeyError, ValueError, TypeError) as error:
            raise ApiError(400, f"bad action: {error}") from error
        return {"ok": True}

    def start_task(self, body: dict[str, Any]) -> dict:
        text = str(body.get("task", "")).strip()
        if not text:
            raise ApiError(400, "missing 'task'")
        mode = body.get("mode", "fast")
        if mode not in ("fast", "reliable"):
            raise ApiError(400, "mode must be fast or reliable")
        udid = body.get("device") or resolve_target().udid
        device = self.device(udid, start=True)
        try:
            entry = self.tasks.start(text, device, max_steps=int(body.get("maxSteps", 30)), mode=mode,
                                     model=body.get("model"))
        except RuntimeError as error:
            raise ApiError(409, str(error)) from error
        return task_dict(entry)

    def task(self, run_id: str) -> Task:
        entry = self.tasks.get(run_id)
        if entry is None:
            raise ApiError(404, f"unknown task {run_id}")
        return entry


def task_dict(entry: Task, with_steps: bool = True) -> dict:
    data: dict[str, Any] = {
        "runId": entry.run_id, "task": entry.task, "device": entry.device, "udid": entry.udid,
        "mode": entry.mode, "status": entry.status, "plan": plan_to_dicts(entry.plan),
        "summary": entry.result.summary if entry.result else entry.error,
        "succeeded": bool(entry.result and entry.result.status == "success"),
        "stepCount": len(entry.steps),
    }
    if entry.result:
        data.update(seconds=entry.result.seconds, usage=asdict(entry.result.usage), plan=entry.result.plan)
    if with_steps:
        data["steps"] = [asdict(step) for step in entry.steps]
    return data


def list_traces(limit: int = 50) -> list[dict]:
    runs = []
    for path in sorted(TRACES_DIR.glob("*/run.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        runs.append({key: data.get(key) for key in
                     ("run_id", "task", "status", "summary", "mode", "device", "steps", "seconds", "started_at")})
    return runs


def trace_dir(run_id: str) -> Path:
    path = TRACES_DIR / run_id
    if not RUN_ID.match(run_id) or not (path / "run.json").exists():
        raise ApiError(404, f"unknown trace {run_id}")
    return path


def read_trace(run_id: str) -> dict:
    path = trace_dir(run_id)
    data = json.loads((path / "run.json").read_text())
    steps_file = path / "steps.jsonl"
    data["steps_detail"] = [json.loads(line) for line in steps_file.read_text().splitlines() if line.strip()] \
        if steps_file.exists() else []
    return data


def create_app(console: Console | None = None) -> Starlette:
    console = console or Console()

    async def call(fn, *args):
        try:
            return await run_in_threadpool(fn, *args)
        except ApiError:
            raise
        except GhosthandError as error:
            raise ApiError(502, str(error)) from error

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError as error:
            raise ApiError(400, "invalid JSON body") from error
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def endpoint(fn):
        async def wrapper(request: Request) -> Response:
            try:
                return await fn(request)
            except ApiError as error:
                return JSONResponse({"error": error.message}, status_code=error.status)
        return wrapper

    @endpoint
    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    @endpoint
    async def devices(request: Request) -> Response:
        return JSONResponse(await call(console.devices))

    @endpoint
    async def runner_control(request: Request) -> Response:
        udid = request.path_params["udid"]
        action = (await body(request)).get("action")
        if action == "start":
            device = await call(console.device, udid, True)
            return JSONResponse({"ok": True, "url": device.client.base_url})
        if action == "stop":
            console.forget(udid)
            return JSONResponse({"ok": await call(runner.stop, udid)})
        raise ApiError(400, "action must be start or stop")

    @endpoint
    async def screen(request: Request) -> Response:
        jpeg = await call(console.screen_jpeg, request.path_params["udid"])
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @endpoint
    async def elements(request: Request) -> Response:
        return JSONResponse(await call(console.elements, request.path_params["udid"]))

    @endpoint
    async def action(request: Request) -> Response:
        return JSONResponse(await call(console.action, request.path_params["udid"], await body(request)))

    @endpoint
    async def tasks(request: Request) -> Response:
        if request.method == "POST":
            return JSONResponse(await call(console.start_task, await body(request)), status_code=201)
        entries = sorted(console.tasks.all(), key=lambda t: t.run_id, reverse=True)
        return JSONResponse([task_dict(t, with_steps=False) for t in entries])

    @endpoint
    async def task(request: Request) -> Response:
        return JSONResponse(task_dict(console.task(request.path_params["run_id"])))

    @endpoint
    async def task_cancel(request: Request) -> Response:
        entry = console.task(request.path_params["run_id"])
        entry.cancel.set()
        return JSONResponse({"ok": True})

    @endpoint
    async def traces(request: Request) -> Response:
        return JSONResponse(await call(list_traces))

    @endpoint
    async def trace(request: Request) -> Response:
        return JSONResponse(await call(read_trace, request.path_params["run_id"]))

    @endpoint
    async def trace_file(request: Request) -> Response:
        name = request.path_params["name"]
        if not TRACE_FILE.match(name):
            raise ApiError(404, "not a trace screenshot")
        path = trace_dir(request.path_params["run_id"]) / name
        if not path.exists():
            raise ApiError(404, "no such screenshot")
        return FileResponse(path, media_type="image/jpeg")

    routes = [
        Route("/", index),
        Route("/api/devices", devices),
        Route("/api/devices/{udid}/runner", runner_control, methods=["POST"]),
        Route("/api/devices/{udid}/screen.jpg", screen),
        Route("/api/devices/{udid}/elements", elements),
        Route("/api/devices/{udid}/action", action, methods=["POST"]),
        Route("/api/tasks", tasks, methods=["GET", "POST"]),
        Route("/api/tasks/{run_id}", task),
        Route("/api/tasks/{run_id}/cancel", task_cancel, methods=["POST"]),
        Route("/api/traces", traces),
        Route("/api/traces/{run_id}", trace),
        Route("/api/traces/{run_id}/{name}", trace_file),
    ]
    app = Starlette(routes=routes)
    return LocalOnly(app)
