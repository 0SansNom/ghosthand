import json
import os
import secrets
import signal
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import physical
from .errors import GhosthandError
from .paths import DERIVED_DATA, RUNNER_PROJECT, RUNNERS_DIR
from .targets import Target, list_apps

DEFAULT_PORT = 8100
# Free-account provisioning profiles expire after 7 days; rebuild a little before.
DEVICE_BUILD_MAX_AGE = 6 * 24 * 3600


class RunnerError(GhosthandError):
    pass


class RunnerClient:
    def __init__(self, base_url: str, timeout: float = 30.0, token: str = ""):
        self.base_url = base_url
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(base_url=base_url, timeout=timeout, headers=headers)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as error:
            raise RunnerError(f"runner unreachable at {self._http.base_url}: {error}") from error
        if response.status_code != 200:
            try:
                message = response.json()["error"]
            except (ValueError, KeyError):
                message = response.text
            raise RunnerError(message)
        return response

    def _post(self, path: str, **body) -> dict:
        return self._request("POST", path, json=body).json()

    def health(self) -> dict:
        return self._request("GET", "/health").json()

    def is_up(self) -> bool:
        try:
            self.health()
            return True
        except RunnerError:
            return False

    def screen(self) -> dict:
        return self._request("GET", "/screen").json()

    def screenshot(self, fast: bool = False) -> bytes:
        """`fast`: point-sized JPEG encoded on the device, far smaller than the full PNG."""
        params = {"format": "jpeg"} if fast else None
        return self._request("GET", "/screenshot", params=params).content

    def tree(self, bundle_id: str | None = None, max_depth: int = 60) -> dict:
        params = {"maxDepth": max_depth}
        if bundle_id:
            params["bundleId"] = bundle_id
        return self._request("GET", "/tree", params=params).json()

    def tap(self, x: float, y: float, duration: float | None = None) -> None:
        body = {"x": float(x), "y": float(y)}
        if duration:
            body["duration"] = float(duration)
        self._post("/tap", **body)

    def swipe(self, from_x: float, from_y: float, to_x: float, to_y: float, duration: float = 0.3) -> None:
        self._post("/swipe", fromX=float(from_x), fromY=float(from_y), toX=float(to_x), toY=float(to_y),
                   duration=float(duration))

    def type_text(self, text: str) -> None:
        self._post("/type", text=text)

    def press(self, button: str) -> None:
        self._post("/press", button=button)

    def launch(self, bundle_id: str, relaunch: bool = False) -> dict:
        return self._post("/launch", bundleId=bundle_id, relaunch=relaunch)

    def terminate(self, bundle_id: str) -> None:
        self._post("/terminate", bundleId=bundle_id)

    def app_states(self, bundle_ids: list[str]) -> dict[str, str]:
        return self._post("/app-state", bundleIds=bundle_ids)["states"]

    def open_url(self, url: str) -> None:
        self._post("/open-url", url=url)

    def set_current_app(self, bundle_id: str | None) -> None:
        self._post("/current-app", bundleId=bundle_id)

    def set_known_apps(self, bundle_ids: list[str]) -> None:
        self._post("/known-apps", bundleIds=bundle_ids)

    def shutdown(self) -> None:
        self._post("/shutdown")


@dataclass
class RunnerState:
    udid: str
    port: int
    pid: int
    log: str
    url: str = ""
    token: str = ""

    @property
    def base_url(self) -> str:
        return self.url or f"http://127.0.0.1:{self.port}"


def _state_file(udid: str) -> Path:
    return RUNNERS_DIR / f"{udid}.json"


def _save_state(state: RunnerState) -> None:
    # Owner-only: the file holds the token that unlocks the runner.
    fd = os.open(_state_file(state.udid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(asdict(state)))


def load_state(udid: str) -> RunnerState | None:
    try:
        return RunnerState(**json.loads(_state_file(udid).read_text()))
    except (FileNotFoundError, ValueError, TypeError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _pick_port() -> int:
    taken = set()
    for path in RUNNERS_DIR.glob("*.json"):
        try:
            taken.add(json.loads(path.read_text())["port"])
        except (ValueError, KeyError):
            pass
    port = DEFAULT_PORT
    while port in taken or not _port_free(port):
        port += 1
    return port


def products_dir(kind: str) -> Path:
    return DERIVED_DATA / kind


def simulator_products() -> Path:
    return products_dir("simulator")


def find_xctestrun(products: Path) -> Path | None:
    matches = sorted((products / "Build" / "Products").glob("*.xctestrun"))
    return matches[0] if matches else None


def bundle_prefix(team: str) -> str:
    """Bundle IDs must be unique per Apple account, so they embed the team ID."""
    return f"dev.ghosthand.{team.lower()}"


def build(target: Target) -> Path:
    products = products_dir(target.kind)
    command = ["xcodebuild", "build-for-testing", "-project", str(RUNNER_PROJECT), "-scheme", "GhosthandRunner",
               "-derivedDataPath", str(products)]
    if target.kind == "simulator":
        command += ["-destination", "generic/platform=iOS Simulator", "CODE_SIGNING_ALLOWED=NO"]
    else:
        team = physical.signing_team()
        if team is None:
            raise RunnerError(
                "No signing team. Add your Apple ID in Xcode > Settings > Accounts "
                "(a free account works), or set GHOSTHAND_TEAM."
            )
        # Registers the device and creates the provisioning profiles on first build.
        command += ["-destination", f"id={target.udid}", "-allowProvisioningUpdates",
                    f"DEVELOPMENT_TEAM={team}", f"GHOSTHAND_BUNDLE_PREFIX={bundle_prefix(team)}"]
    log = RUNNERS_DIR / f"build-{target.kind}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as out:
        result = subprocess.run(command, stdout=out, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RunnerError(f"runner build failed, see {log}:\n{_build_errors(log)}")
    xctestrun = find_xctestrun(products)
    if xctestrun is None:
        raise RunnerError(f"build succeeded but no .xctestrun found under {products}")
    return xctestrun


def build_for_simulator() -> Path:
    from .simctl import Simulator

    return build(Simulator("", "", "", ""))


def _needs_build(target: Target, xctestrun: Path | None) -> bool:
    if xctestrun is None or _sources_newer_than(xctestrun):
        return True
    return target.kind == "device" and time.time() - xctestrun.stat().st_mtime > DEVICE_BUILD_MAX_AGE


def _sources_newer_than(product: Path) -> bool:
    built_at = product.stat().st_mtime
    sources = RUNNER_PROJECT.parent
    return any(path.stat().st_mtime > built_at for path in sources.rglob("*") if path.is_file())


def _tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return ""


def _build_errors(log: Path) -> str:
    errors = [line for line in _tail(log, 400).splitlines() if "error:" in line]
    return "\n".join(errors[:10]) or _tail(log)


TRUST_HINT = ("On the iPhone: Settings > General > VPN & Device Management > your \"Apple Development\" "
              "certificate > Trust, then start again.")
UNLOCK_HINT = "Unlock the iPhone: Xcode waits for it before launching the runner."
DEVICE_HINTS = {
    "is locked": UNLOCK_HINT,
    "Unlock": UNLOCK_HINT,
    "not been explicitly trusted": TRUST_HINT,
    "Untrusted Developer": TRUST_HINT,
    "Developer Mode": "Turn on Developer Mode: Settings > Privacy & Security > Developer Mode.",
    "UI Automation": "Turn on UI automation: Settings > Developer > Enable UI Automation.",
}


def _hints(log: Path) -> list[str]:
    text = _tail(log, 200)
    return list(dict.fromkeys(hint for marker, hint in DEVICE_HINTS.items() if marker in text))


def client_from(state: RunnerState, timeout: float = 30.0) -> RunnerClient:
    return RunnerClient(state.base_url, timeout=timeout, token=state.token)


def client_for(udid: str, timeout: float = 30.0) -> RunnerClient:
    state = load_state(udid)
    if state is None:
        raise RunnerError(f"no runner started for {udid}; run `ghosthand runner start`")
    return client_from(state, timeout)


def _reusable(state: RunnerState | None) -> RunnerState | None:
    """A runner we can keep: its process answers, and it knows the current token."""
    if state and state.token and _pid_alive(state.pid) and client_from(state, timeout=5.0).is_up():
        return state
    return None


def start(target: Target, timeout: float | None = None,
          on_wait: Callable[[str], None] | None = None) -> RunnerState:
    udid = target.udid
    existing = load_state(udid)
    if (kept := _reusable(existing)) is not None:
        return kept
    # A live process that no longer answers would sit next to the new one.
    if existing is not None:
        stop(udid)

    xctestrun = find_xctestrun(products_dir(target.kind))
    if _needs_build(target, xctestrun):
        xctestrun = build(target)
    if target.kind == "simulator":
        port = _pick_port()
        url = f"http://127.0.0.1:{port}"
    else:
        # Reached through Xcode's CoreDevice tunnel, which xcodebuild keeps open while the test runs.
        # Its address is only known once the tunnel is up, so it is resolved while waiting.
        port = DEFAULT_PORT
        url = ""
    timeout = timeout or (180.0 if target.kind == "simulator" else 300.0)

    RUNNERS_DIR.mkdir(parents=True, exist_ok=True)
    log = RUNNERS_DIR / f"{udid}.log"
    token = secrets.token_urlsafe(24)
    # xcodebuild forwards TEST_RUNNER_* variables to the test process without the prefix.
    env = {**os.environ, "TEST_RUNNER_GHOSTHAND_PORT": str(port), "TEST_RUNNER_GHOSTHAND_TOKEN": token}
    with log.open("wb") as out:
        process = subprocess.Popen(
            ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun), "-destination", f"id={udid}"],
            stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
    state = RunnerState(udid=udid, port=port, pid=process.pid, log=str(log), url=url, token=token)
    _save_state(state)

    client = client_from(state, timeout=5.0) if url else None
    shown: set[str] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client is None and (address := physical.tunnel_address(udid)):
            state.url = f"http://[{address}]:{port}"
            _save_state(state)
            client = client_from(state, timeout=5.0)
        if process.poll() is not None:
            _state_file(udid).unlink(missing_ok=True)
            hints = "\n".join(_hints(log))
            raise RunnerError(f"runner exited during startup, see {log}\n{hints or _tail(log)}")
        if client and client.is_up():
            _sync_known_apps(target, client)
            return state
        for hint in _hints(log):
            if on_wait and hint not in shown:
                shown.add(hint)
                on_wait(hint)
        time.sleep(1)
    stop(udid)
    hints = "\n".join(_hints(log))
    raise RunnerError(f"runner did not answer within {timeout:.0f}s, see {log}\n{hints}".rstrip())


def _sync_known_apps(target: Target, client: RunnerClient) -> None:
    """Tell the runner which apps exist so it can tell which one is in front."""
    client.set_known_apps(sorted(list_apps(target)))


def _reap(pid: int, timeout: float = 5.0) -> None:
    """Stop a runner process group and wait until it has released its port."""
    if not _pid_alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def stop(udid: str) -> bool:
    state = load_state(udid)
    if state is None:
        return False
    try:
        client_from(state, timeout=5.0).shutdown()
    except RunnerError:
        pass
    _reap(state.pid)
    _state_file(udid).unlink(missing_ok=True)
    return True
