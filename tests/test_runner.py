import json

import pytest

from ghosthand import runner
from ghosthand.device import Device
from ghosthand.runner import RunnerState
from ghosthand.simctl import Simulator


def target():
    return Simulator("U", "iPhone", "Booted", "iOS 27.1")


def test_client_sends_the_bearer_token():
    client = runner.RunnerClient("http://127.0.0.1:9", token="secret")
    assert client._http.headers["authorization"] == "Bearer secret"


def test_load_state_tolerates_a_file_written_before_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUNNERS_DIR", tmp_path)
    (tmp_path / "U.json").write_text(json.dumps({"udid": "U", "port": 1, "pid": 2, "log": "l"}))
    state = runner.load_state("U")
    assert state is not None and state.token == ""


def test_reusable_needs_a_token_and_an_answer(monkeypatch):
    state = RunnerState("U", 1, 2, "log", url="http://127.0.0.1:1", token="t")
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(runner.RunnerClient, "is_up", lambda self: True)
    assert runner._reusable(state) is state

    assert runner._reusable(RunnerState("U", 1, 2, "log", url="http://127.0.0.1:1")) is None
    monkeypatch.setattr(runner.RunnerClient, "is_up", lambda self: False)
    assert runner._reusable(state) is None


def test_start_stops_a_mute_runner_before_launching(monkeypatch):
    state = RunnerState("U", 8100, 99, "log", url="http://127.0.0.1:8100", token="t")
    monkeypatch.setattr(runner, "load_state", lambda udid: state)
    stopped = []
    monkeypatch.setattr(runner, "stop", lambda udid: stopped.append(udid) or True)
    monkeypatch.setattr(runner, "_reusable", lambda current: None)
    monkeypatch.setattr(runner, "find_xctestrun", lambda products: (_ for _ in ()).throw(RuntimeError("past stop")))

    with pytest.raises(RuntimeError, match="past stop"):
        runner.start(target())
    assert stopped == ["U"]


def test_start_keeps_a_runner_that_answers(monkeypatch):
    state = RunnerState("U", 8100, 99, "log", url="http://127.0.0.1:8100", token="t")
    monkeypatch.setattr(runner, "load_state", lambda udid: state)
    monkeypatch.setattr(runner, "_reusable", lambda current: current)
    monkeypatch.setattr(runner, "stop", lambda udid: (_ for _ in ()).throw(AssertionError("stopped")))
    assert runner.start(target()) is state


class _Screen:
    def __init__(self):
        self.calls = []

    def screen(self):
        return {"width": 100, "height": 100}

    def tree(self, bundle_id=None):
        return {"type": "Button", "label": "OK", "frame": {"x": 0, "y": 0, "width": 20, "height": 10}}

    def tap(self, x, y, duration=None):
        self.calls.append((x, y, duration))


def test_tap_element_forwards_the_hold():
    client = _Screen()
    device = Device(target(), client)
    assert device.tap_element(0, 1.5).label == "OK"
    assert client.calls == [(10.0, 5.0, 1.5)]
