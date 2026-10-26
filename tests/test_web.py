import json

import pytest
from starlette.testclient import TestClient

from ghosthand import web


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "TRACES_DIR", tmp_path)
    console = web.Console()
    client = TestClient(web.create_app(console), base_url="http://localhost:8000")
    return client, console, tmp_path


def write_trace(root, run_id="20261026-100000-abcdef"):
    trace = root / run_id
    trace.mkdir()
    (trace / "run.json").write_text(json.dumps({"run_id": run_id, "task": "Open General", "status": "success",
                                                "steps": 1, "mode": "fast"}))
    step = {"number": 1, "action": "finish", "input": {}, "screenshot": "step-001.jpg", "ok": True}
    (trace / "steps.jsonl").write_text(json.dumps(step) + "\n")
    (trace / "step-001.jpg").write_bytes(b"\xff\xd8jpeg")
    return run_id


def test_index_and_traces(setup):
    client, _, root = setup
    run_id = write_trace(root)
    assert "Ghosthand" in client.get("/").text
    assert client.get("/api/traces").json()[0]["task"] == "Open General"
    detail = client.get(f"/api/traces/{run_id}").json()
    assert detail["steps_detail"][0]["action"] == "finish"
    assert client.get(f"/api/traces/{run_id}/step-001.jpg").content == b"\xff\xd8jpeg"


def test_trace_paths_cannot_escape(setup):
    client, _, root = setup
    run_id = write_trace(root)
    (root / "secret.txt").write_text("nope")
    assert client.get(f"/api/traces/{run_id}/run.json").status_code == 404
    assert client.get(f"/api/traces/{run_id}/..%2Fsecret.txt").status_code == 404
    assert client.get("/api/traces/..").status_code == 404


def test_local_only_and_json_writes(setup):
    client, _, _ = setup
    assert client.get("/api/traces", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/tasks", content="{}", headers={"Content-Type": "text/plain"}).status_code == 403
    cross = client.post("/api/tasks", json={"task": "x"}, headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403
    same = client.post("/api/tasks", json={"task": ""}, headers={"Origin": "http://localhost:8000"})
    assert same.status_code == 400 and same.json()["error"] == "missing 'task'"


def test_task_validation(setup):
    client, _, _ = setup
    assert client.post("/api/tasks", json={"task": "x", "mode": "turbo"}).status_code == 400
    assert client.post("/api/tasks", json=["not", "an", "object"]).status_code == 400
    assert client.get("/api/tasks/unknown").status_code == 404




def test_device_errors_are_json(setup, monkeypatch):
    client, console, _ = setup

    def unavailable(udid, start=False):
        raise web.ApiError(409, "no runner started for U9")
    monkeypatch.setattr(console, "device", unavailable)
    response = client.get("/api/devices/U9/screen.jpg")
    assert response.status_code == 409 and "no runner" in response.json()["error"]
