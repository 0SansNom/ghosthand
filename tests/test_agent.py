import io
import json
import threading

from PIL import Image

from ghosthand.agent import TOOLS, Agent
from ghosthand.device import Device
from ghosthand.llm import Decision, Judgment, Usage
from ghosthand.simctl import Simulator


def png(width=40, height=80) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


class FakeClient:
    """Two screens: a list with a "Général" button, and the page it opens."""

    def __init__(self):
        self.page = "home"
        self.taps = []

    def screen(self):
        return {"width": 400, "height": 800, "scale": 3, "orientation": "portrait"}

    def screenshot(self, fast=False):
        return png()

    def tree(self, bundle_id=None):
        label = "Général" if self.page == "home" else "Informations"
        return {"type": "Application", "bundleId": "com.apple.Preferences", "frame": self._frame(0, 0, 400, 800),
                "children": [{"type": "Button", "label": label, "frame": self._frame(0, 100, 400, 50)}]}

    def tap(self, x, y, duration=None):
        self.taps.append((x, y))
        self.page = "general"

    @staticmethod
    def _frame(x, y, w, h):
        return {"x": x, "y": y, "width": w, "height": h}


class Scripted:
    model = "scripted"

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []

    def decide(self, system, text, image, tools):
        self.prompts.append(text)
        return self.decisions.pop(0)


def make_agent(tmp_path, decisions, **kwargs):
    client = FakeClient()
    device = Device(Simulator("UDID", "iPhone Test", "Booted", "iOS 27.1"), client)
    llm = Scripted(decisions)
    agent = Agent(device, llm, traces_dir=tmp_path, **kwargs)
    agent._apps_block = lambda: "Installed apps: com.apple.Preferences  Réglages"
    return agent, client, llm


def test_successful_run_writes_trace(tmp_path, monkeypatch):
    monkeypatch.setattr("ghosthand.agent.SETTLE_SECONDS", 0)
    agent, client, llm = make_agent(tmp_path, [
        Decision("tap_element", {"index": 0, "reason": "open it"}, usage=Usage(100, 20, 50, 0)),
        Decision("finish", {"success": True, "summary": "done"}, usage=Usage(10, 5)),
    ])
    result = agent.run("Open General", run_id="run1")

    assert (result.status, result.summary) == ("success", "done")
    assert client.taps == [(200, 125)]
    assert result.usage.input_tokens == 110 and result.usage.cache_read_tokens == 50
    assert '[0] Button "Informations"' in llm.prompts[1]
    assert "Step 1: tap_element(index=0) -> ok" in llm.prompts[1]

    trace = tmp_path / "run1"
    run = json.loads((trace / "run.json").read_text())
    assert run["status"] == "success" and run["steps"] == 2
    steps = [json.loads(line) for line in (trace / "steps.jsonl").read_text().splitlines()]
    assert [s["action"] for s in steps] == ["tap_element", "finish"]
    assert (trace / "step-001.jpg").read_bytes()[:2] == b"\xff\xd8"


def test_bad_index_is_reported_to_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr("ghosthand.agent.SETTLE_SECONDS", 0)
    agent, client, llm = make_agent(tmp_path, [
        Decision("tap_element", {"index": 7, "reason": "oops"}),
        Decision("finish", {"success": False, "summary": "gave up"}),
    ])
    result = agent.run("Open General")
    assert result.status == "failed"
    assert client.taps == []
    assert "error: no element [7] on this screen (the screen did not change)" in llm.prompts[1]


def test_stops_after_max_steps_and_when_model_stays_silent(tmp_path):
    agent, _, _ = make_agent(tmp_path, [Decision("wait", {"seconds": 0.5, "reason": "loading"})] * 2, max_steps=2)
    assert agent.run("Wait forever").status == "max_steps"

    agent, _, _ = make_agent(tmp_path, [Decision(None, text="hmm")] * 3)
    result = agent.run("Say nothing")
    assert (result.status, result.summary) == ("error", "The model stopped calling tools.")


def test_cancel_and_refusal(tmp_path):
    cancel = threading.Event()
    cancel.set()
    agent, _, _ = make_agent(tmp_path, [], cancel=cancel)
    assert agent.run("Anything").status == "cancelled"

    agent, _, _ = make_agent(tmp_path, [Decision(None, text="Request declined (cyber).", stop_reason="refusal")])
    result = agent.run("Anything")
    assert (result.status, result.summary) == ("error", "Request declined (cyber).")


def test_tool_schemas_are_strict():
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert tool["strict"] and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


class Reviewer(Scripted):
    """Scripted operator plus scripted structured answers (plan, checks)."""

    def __init__(self, decisions, judgments):
        super().__init__(decisions)
        self.judgments = list(judgments)
        self.judged = []

    def judge(self, system, text, image, schema, effort=None):
        self.judged.append(text)
        return Judgment(self.judgments.pop(0), Usage(1, 1))


PLAN = {"checkpoints": [{"goal": "General open", "evidence": "Informations row"}], "notes": ""}


def test_reliable_mode_plans_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr("ghosthand.agent.SETTLE_SECONDS", 0)
    client = FakeClient()
    device = Device(Simulator("UDID", "iPhone Test", "Booted", "iOS 27.1"), client)
    llm = Reviewer(
        [
            Decision("checkpoint_done", {"index": 0, "evidence": "too early"}),
            Decision("tap_element", {"index": 0, "reason": "open General"}),
            Decision("checkpoint_done", {"index": 0, "evidence": "Informations visible"}),
            Decision("finish", {"success": True, "summary": "General is open"}),
        ],
        [
            PLAN,
            {"verified": False, "reason": "still on the list"},
            {"verified": True, "reason": "Informations is shown"},
            {"verified": True, "reason": "General page visible"},
        ],
    )
    agent = Agent(device, llm, traces_dir=tmp_path, mode="reliable")
    agent._apps_block = lambda: "apps"
    result = agent.run("Open General", run_id="r1")

    assert result.status == "success"
    assert result.plan == [{"goal": "General open", "evidence": "Informations row", "done": True}]
    assert "[>] 0. General open" in llm.prompts[0]
    assert "checkpoint 0 NOT verified: still on the list" in llm.prompts[1]
    assert "[x] 0. General open" in llm.prompts[3]
    assert [s.check["verified"] for s in result.steps if s.check] == [False, True, True]


def test_reliable_mode_fails_after_repeated_unverified_finish(tmp_path):
    agent, _, llm = make_agent(tmp_path, [Decision("finish", {"success": True, "summary": "done"})] * 2,
                               mode="reliable")
    llm.judge = Reviewer([], [PLAN, {"verified": False, "reason": "nothing changed"},
                              {"verified": False, "reason": "nothing changed"}]).judge
    result = agent.run("Open General")
    assert result.status == "failed" and "not verified: nothing changed" in result.summary



class MovingClient(FakeClient):
    """The screen changes between the agent's observation and its tap."""

    def __init__(self, later_children):
        super().__init__()
        self.reads = 0
        self.later_children = later_children

    def tree(self, bundle_id=None):
        self.reads += 1
        if self.reads == 1:
            return super().tree(bundle_id)
        return {"type": "Application", "bundleId": "com.apple.Preferences", "frame": self._frame(0, 0, 400, 800),
                "children": self.later_children}


def run_with(tmp_path, monkeypatch, client):
    monkeypatch.setattr("ghosthand.agent.SETTLE_SECONDS", 0)
    device = Device(Simulator("UDID", "iPhone Test", "Booted", "iOS 27.1"), client)
    llm = Scripted([Decision("tap_element", {"index": 0, "reason": "open"}),
                    Decision("finish", {"success": False, "summary": "stop"})])
    agent = Agent(device, llm, traces_dir=tmp_path)
    agent._apps_block = lambda: "apps"
    agent.run("Open General")
    return llm


def test_tap_follows_an_element_that_moved(tmp_path, monkeypatch):
    client = MovingClient([{"type": "Button", "label": "Général", "frame": FakeClient._frame(0, 300, 400, 50)}])
    llm = run_with(tmp_path, monkeypatch, client)
    assert client.taps == [(200, 325)]
    assert "tapped its new position" in llm.prompts[1]


def test_tap_is_skipped_when_the_element_vanished(tmp_path, monkeypatch):
    client = MovingClient([{"type": "Button", "label": "Annuler", "frame": FakeClient._frame(0, 100, 400, 50)}])
    llm = run_with(tmp_path, monkeypatch, client)
    assert client.taps == []
    assert "is no longer on screen; nothing was tapped" in llm.prompts[1]
