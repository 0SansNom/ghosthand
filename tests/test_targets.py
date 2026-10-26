import pytest

from ghosthand import targets
from ghosthand.physical import PhysicalDevice, parse_devices
from ghosthand.simctl import Simulator
from ghosthand.targets import TargetError, resolve_target

DEVICECTL = {
    "devices": [
        {   # devicectl also lists simulators; they must be skipped
            "identifier": "SIM", "hardwareProperties": {"reality": "simulated", "platform": "iOS"},
            "deviceProperties": {"name": "iPhone 17"}, "connectionProperties": {},
        },
        {
            "identifier": "91EC", "hardwareProperties": {"reality": "physical", "platform": "iOS", "udid": "0000-AAA"},
            "deviceProperties": {"name": "iPhone de test", "osVersionNumber": "27.2", "developerModeStatus": "enabled"},
            "connectionProperties": {"pairingState": "paired", "transportType": "wired"},
        },
        {
            "identifier": "OLD", "hardwareProperties": {"reality": "physical", "platform": "iOS", "udid": "0000-BBB"},
            "deviceProperties": {"name": "Vieil iPhone", "osVersionNumber": "26.0"},
            "connectionProperties": {"pairingState": "paired"},
        },
    ]
}


def test_parse_devices_keeps_physical_iphones():
    phones = parse_devices(DEVICECTL)
    assert [(p.udid, p.name, p.runtime, p.booted) for p in phones] == [
        ("0000-AAA", "iPhone de test", "iOS 27.2", True),
        ("0000-BBB", "Vieil iPhone", "iOS 26.0", False),  # paired but not connected
    ]
    assert phones[0].kind == "device"


def sim(udid, name, booted):
    return Simulator(udid, name, "Booted" if booted else "Shutdown", "iOS 27.1")


def phone(udid, name, reachable):
    return PhysicalDevice(udid, name, "iOS 27.2", "paired", "wired" if reachable else None, "enabled")


@pytest.fixture
def fleet(monkeypatch):
    def use(*items):
        monkeypatch.setattr(targets, "list_targets", lambda: list(items))
    monkeypatch.delenv("GHOSTHAND_DEVICE", raising=False)
    return use


def test_booted_simulator_wins_over_reachable_iphone(fleet):
    fleet(sim("S1", "iPhone 17", True), phone("P1", "Mon iPhone", True))
    assert resolve_target().udid == "S1"


def test_iphone_is_the_default_when_no_simulator_runs(fleet):
    fleet(sim("S1", "iPhone 17", False), phone("P1", "Mon iPhone", True))
    assert resolve_target().udid == "P1"


def test_select_by_name_or_udid(fleet, monkeypatch):
    fleet(sim("S1", "iPhone 17", True), phone("P1", "Mon iPhone", True))
    assert resolve_target("mon iphone").udid == "P1"
    assert resolve_target("S1").udid == "S1"
    monkeypatch.setenv("GHOSTHAND_DEVICE", "Mon iPhone")
    assert resolve_target().udid == "P1"


def test_ambiguous_or_missing_targets(fleet):
    fleet(sim("S1", "iPhone 17", True), sim("S2", "iPhone Air", True))
    with pytest.raises(TargetError, match="several simulators"):
        resolve_target()
    fleet(sim("S1", "iPhone 17", False))
    with pytest.raises(TargetError, match="no booted simulator"):
        resolve_target()
    with pytest.raises(TargetError, match="matches 'Pixel'"):
        resolve_target("Pixel")
