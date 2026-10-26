import json
import os
import plistlib
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import GhosthandError


class PhysicalError(GhosthandError):
    pass


@dataclass
class PhysicalDevice:
    udid: str
    name: str
    runtime: str
    state: str
    transport: str | None
    developer_mode: str | None
    kind: str = "device"

    @property
    def booted(self) -> bool:
        return self.state == "paired" and self.transport is not None


def _devicectl_json(*args: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.json"
        result = subprocess.run(["xcrun", "devicectl", *args, "--json-output", str(out)],
                                capture_output=True, text=True)
        if result.returncode != 0 or not out.exists():
            message = result.stderr.strip() or result.stdout.strip()
            raise PhysicalError(f"devicectl {args[0]} {args[1]} failed: {message}")
        return json.loads(out.read_text())["result"]


def parse_devices(result: dict) -> list[PhysicalDevice]:
    devices = []
    for entry in result.get("devices", []):
        hardware = entry.get("hardwareProperties", {})
        if hardware.get("reality") != "physical" or hardware.get("platform", "iOS") != "iOS":
            continue
        props = entry.get("deviceProperties", {})
        connection = entry.get("connectionProperties", {})
        devices.append(PhysicalDevice(
            udid=hardware.get("udid") or entry["identifier"],
            name=props.get("name", "iPhone"),
            runtime=f"iOS {props.get('osVersionNumber', '?')}",
            state=connection.get("pairingState", "unknown"),
            transport=connection.get("transportType"),
            developer_mode=props.get("developerModeStatus"),
        ))
    return devices


def list_devices() -> list[PhysicalDevice]:
    try:
        return parse_devices(_devicectl_json("list", "devices"))
    except (PhysicalError, FileNotFoundError):
        return []  # no Xcode device support: simulators still work


def list_apps(udid: str) -> dict[str, dict]:
    result = _devicectl_json("device", "info", "apps", "--device", udid, "--include-all-apps")
    return {
        app["bundleIdentifier"]: {"CFBundleDisplayName": app.get("name", "")}
        for app in result.get("apps", [])
        if not app.get("hidden") and not app.get("appClip")
    }


def tunnel_host(udid: str) -> str:
    # Resolves only while Xcode's CoreDevice tunnel to the device is up (USB or Wi-Fi).
    return f"{udid}.coredevice.local"


def tunnel_address(udid: str) -> str | None:
    # Resolve once and connect by address: each `.coredevice.local` lookup goes through mDNS and
    # takes seconds, which would make every runner request slow.
    try:
        infos = socket.getaddrinfo(tunnel_host(udid), None, socket.AF_INET6, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    return infos[0][4][0] if infos else None


def signing_team() -> str | None:
    if team := os.environ.get("GHOSTHAND_TEAM"):
        return team
    exported = subprocess.run(["defaults", "export", "com.apple.dt.Xcode", "-"], capture_output=True)
    if exported.returncode != 0:
        return None
    teams_by_account = plistlib.loads(exported.stdout).get("IDEProvisioningTeamByIdentifier", {})
    teams = [team for account in teams_by_account.values() for team in account]
    # Prefer a paid team when both exist: its profiles last a year instead of 7 days.
    teams.sort(key=lambda team: bool(team.get("isFreeProvisioningTeam")))
    return teams[0]["teamID"] if teams else None
