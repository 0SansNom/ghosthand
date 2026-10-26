import json
import plistlib
import subprocess
from dataclasses import dataclass

from .errors import GhosthandError


class SimctlError(GhosthandError):
    pass


@dataclass
class Simulator:
    udid: str
    name: str
    state: str
    runtime: str
    kind: str = "simulator"

    @property
    def booted(self) -> bool:
        return self.state == "Booted"


def _run(*args: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["xcrun", "simctl", *args], capture_output=True)
    if result.returncode != 0:
        raise SimctlError(result.stderr.decode().strip() or f"simctl {args[0]} failed")
    return result


def parse_devices(payload: dict) -> list[Simulator]:
    simulators = []
    for runtime, devices in payload.get("devices", {}).items():
        # "com.apple.CoreSimulator.SimRuntime.iOS-27-1" -> "iOS 27.1"
        name = runtime.rsplit(".", 1)[-1].replace("-", " ", 1).replace("-", ".")
        for device in devices:
            if device.get("isAvailable", True):
                simulators.append(Simulator(device["udid"], device["name"], device["state"], name))
    return simulators


def list_simulators() -> list[Simulator]:
    return parse_devices(json.loads(_run("list", "devices", "available", "-j").stdout))


def boot(udid: str) -> None:
    try:
        _run("boot", udid)
    except SimctlError as error:
        if "current state: Booted" not in str(error):
            raise
    _run("bootstatus", udid, "-b")
    developer_dir = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["open", f"{developer_dir}/Applications/Simulator.app"], check=False)


def list_apps(udid: str) -> dict[str, dict]:
    # listapps prints an old-style plist, which plistlib can't read; plutil can.
    raw = _run("listapps", udid).stdout
    converted = subprocess.run(
        ["plutil", "-convert", "xml1", "-o", "-", "-"], input=raw, capture_output=True, check=True
    )
    return plistlib.loads(converted.stdout)


def open_url(udid: str, url: str) -> None:
    _run("openurl", udid, url)
