import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import observe
from .device import LOCKED_MESSAGE, Device, DeviceError
from .errors import GhosthandError
from .llm import Decision, DecisionMaker, Usage
from .planning import Checkpoint, make_plan, plan_to_dicts, render_plan, verify
from .targeting import element_at, moved, point_changed, relocate
from .targets import list_apps
from .paths import TRACES_DIR
from .tree import Element, flatten, render

SYSTEM = """\
You operate an iOS device to complete a task for the user, the way a careful person would.

Each turn you receive the task, a journal of your previous steps and the current screen: a numbered list of \
visible elements and a screenshot where interactive elements carry the same numbers. Call exactly one tool \
per turn.

How the device works:
- Prefer tap_element with an index from the current list. Use tap_point only for things missing from the \
list; screenshot pixels are screen points, so a position read off the screenshot is a valid tap position.
- To type, tap the text field first; it must show as focused before type_text.
- iOS has no back button: tap the back button at the top left of the screen, or swipe_points from \
(2, mid-height) to (250, mid-height).
- Content beyond the screen is reached by swiping; "up" moves the finger up and reveals what is below.
- Open apps with launch_app rather than hunting for icons. Installed apps are listed below.
- After an action, the screen may still be animating or loading; use wait when the journal says the screen \
did not change and you expected it to.

Judgment:
- Do exactly what the task asks, nothing more. Dismiss onboarding, tips and "what's new" screens when they \
stand in the way.
- Never complete a purchase, payment, deletion of user data, or a message to a real person unless the task \
explicitly asks for that exact action. If the task needs one and doesn't clearly authorize it, stop with \
finish(success=false) and explain what confirmation is needed.
- If a login, a missing app or a permission you shouldn't grant blocks the task, finish(success=false) and say so.
- Before finish(success=true), check on the current screen that the task's outcome is actually visible.
- If several different attempts fail to make progress, finish(success=false) rather than looping.
"""

RELIABLE_SYSTEM = """\
Reliable mode: the task comes with a plan of checkpoints, shown in <plan> ([x] done, [>] current).
- Work towards the current checkpoint. When its evidence is visible on screen, call checkpoint_done; an \
independent reviewer checks the claim against the screen, and the checkpoint only counts once verified.
- If the plan no longer fits what you see (unexpected screen, wrong assumption), call replan with the reason.
- finish(success=true) is reviewed the same way: make sure the outcome is visible first.
"""

REASON = {"type": "string", "description": "One short sentence: what you see and why you take this action."}


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("tap_element", "Tap the element with this index in the current element list.",
          {"index": {"type": "integer"}, "reason": REASON}),
    _tool("tap_point", "Tap a screen position in points (top-left origin).",
          {"x": {"type": "number"}, "y": {"type": "number"}, "reason": REASON}),
    _tool("long_press_element", "Press and hold an element, e.g. to open a context menu.",
          {"index": {"type": "integer"}, "seconds": {"type": "number", "description": "Usually 1.0."},
           "reason": REASON}),
    _tool("swipe", "Swipe from the screen center. The direction is the finger's: up reveals content below.",
          {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "reason": REASON}),
    _tool("swipe_points", "Swipe between two points in points, e.g. from the left edge to go back.",
          {"from_x": {"type": "number"}, "from_y": {"type": "number"},
           "to_x": {"type": "number"}, "to_y": {"type": "number"}, "reason": REASON}),
    _tool("type_text", "Type into the focused text field.",
          {"text": {"type": "string"},
           "press_enter": {"type": "boolean", "description": "Press Return after typing (submit, search)."},
           "reason": REASON}),
    _tool("press_home", "Go to the home screen.", {"reason": REASON}),
    _tool("launch_app", "Open an installed app by bundle ID (bring it to front if already running).",
          {"bundle_id": {"type": "string"}, "reason": REASON}),
    _tool("open_url", "Open a URL or deep link.", {"url": {"type": "string"}, "reason": REASON}),
    _tool("wait", "Wait for loading or animations to finish.",
          {"seconds": {"type": "number", "description": "Between 0.5 and 5."}, "reason": REASON}),
    _tool("finish", "End the task. Call it once the outcome is visible on screen, or when you cannot proceed.",
          {"success": {"type": "boolean"},
           "summary": {"type": "string", "description": "What was done, or what blocked the task."}}),
]

RELIABLE_TOOLS = TOOLS + [
    _tool("checkpoint_done", "Claim that a plan checkpoint is reached; it is verified against the current screen.",
          {"index": {"type": "integer"}, "evidence": {"type": "string", "description": "What on screen shows it."}}),
    _tool("replan", "Ask for a new plan when the current one no longer fits the situation.",
          {"reason": {"type": "string"}}),
]

MODES = ("fast", "reliable")
MAX_FINAL_REJECTIONS = 2

SETTLE_SECONDS = 0.6
MAX_SILENT_TURNS = 3


@dataclass
class Step:
    number: int
    app: str
    action: str | None
    input: dict[str, Any]
    reason: str
    outcome: str
    ok: bool
    screenshot: str
    seconds: float
    usage: dict[str, int]
    check: dict | None = None


@dataclass
class RunResult:
    run_id: str
    task: str
    status: str  # success | failed | max_steps | error | cancelled
    summary: str
    steps: list[Step] = field(default_factory=list)
    trace_dir: str = ""
    usage: Usage = field(default_factory=Usage)
    seconds: float = 0.0
    mode: str = "fast"
    plan: list[dict] = field(default_factory=list)


class Trace:
    def __init__(self, root: Path, run_id: str):
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def save_screenshot(self, number: int, jpeg: bytes) -> str:
        name = f"step-{number:03d}.jpg"
        (self.dir / name).write_bytes(jpeg)
        return name

    def add_step(self, step: Step) -> None:
        with (self.dir / "steps.jsonl").open("a") as out:
            out.write(json.dumps(asdict(step), ensure_ascii=False) + "\n")

    def write_run(self, data: dict) -> None:
        (self.dir / "run.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class Agent:
    def __init__(self, device: Device, llm: DecisionMaker, max_steps: int = 30,
                 traces_dir: Path = TRACES_DIR, on_step: Callable[[Step], None] | None = None,
                 cancel: threading.Event | None = None, model_name: str = "", mode: str = "fast",
                 on_plan: Callable[[list[Checkpoint], str], None] | None = None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.device = device
        self.llm = llm
        self.max_steps = max_steps
        self.traces_dir = traces_dir
        self.on_step = on_step
        self.on_plan = on_plan
        self.cancel = cancel or threading.Event()
        self.model_name = model_name or getattr(llm, "model", "")
        self.mode = mode

    def run(self, task: str, run_id: str | None = None) -> RunResult:
        run_id = run_id or new_run_id()
        trace = Trace(self.traces_dir, run_id)
        result = RunResult(run_id=run_id, task=task, status="error", summary="", trace_dir=str(trace.dir),
                           mode=self.mode)
        started = time.monotonic()
        meta = {"run_id": run_id, "task": task, "model": self.model_name, "mode": self.mode,
                "device": self.device.target.name,
                "udid": self.device.target.udid, "started_at": datetime.now().isoformat(timespec="seconds"),
                "max_steps": self.max_steps}
        trace.write_run({**meta, "status": "running"})

        try:
            self._loop(task, trace, result)
        except GhosthandError as error:
            result.status, result.summary = "error", str(error)
        finally:
            result.seconds = round(time.monotonic() - started, 1)
            trace.write_run({**meta, "status": result.status, "summary": result.summary, "plan": result.plan,
                             "steps": len(result.steps), "seconds": result.seconds, "usage": asdict(result.usage),
                             "finished_at": datetime.now().isoformat(timespec="seconds")})
        return result

    def _loop(self, task: str, trace: Trace, result: RunResult) -> None:
        reliable = self.mode == "reliable"
        apps = self._apps_block()
        system = [SYSTEM, RELIABLE_SYSTEM, apps] if reliable else [SYSTEM, apps]
        tools = RELIABLE_TOOLS if reliable else TOOLS
        journal: list[str] = []
        plan: list[Checkpoint] = []
        previous_listing = None
        last_action = None
        silent_turns = 0
        final_rejections = 0

        for number in range(1, self.max_steps + 1):
            if self.cancel.is_set():
                result.status, result.summary = "cancelled", "Cancelled by the user."
                return
            step_started = time.monotonic()
            app, elements, listing, jpeg = self._observe()
            if previous_listing == listing and last_action not in (None, "wait", "checkpoint_done", "replan"):
                journal[-1] += " (the screen did not change)"
            previous_listing = listing
            if reliable and not plan:
                plan = self._plan(task, apps, journal, listing, jpeg, result)

            prompt = _prompt(task, journal, app, listing, number, self.max_steps, plan if reliable else None)
            decision = self.llm.decide(system, prompt, jpeg, tools)
            result.usage.add(decision.usage)
            screenshot = trace.save_screenshot(number, jpeg)

            if decision.stop_reason == "refusal":
                result.status, result.summary = "error", decision.text
                return
            if decision.tool is None:
                silent_turns += 1
                journal.append(f"Step {number}: (no tool called; you must call exactly one tool)")
                if silent_turns >= MAX_SILENT_TURNS:
                    result.status, result.summary = "error", "The model stopped calling tools."
                    return
                continue
            silent_turns = 0
            record = lambda outcome, ok, **extra: self._record(  # noqa: E731
                trace, result, number, app, decision, outcome, ok, screenshot, step_started, **extra)

            if decision.tool == "finish":
                success = bool(decision.input.get("success"))
                summary = str(decision.input.get("summary", ""))
                if success and reliable:
                    check = verify(self.llm, task, f"The task is complete: {summary}", journal, listing, jpeg)
                    result.usage.add(check.usage)
                    if not check.verified:
                        final_rejections += 1
                        outcome = f"final check failed: {check.reason}"
                        record(outcome, False, check={"claim": "finish", "verified": False, "reason": check.reason})
                        if final_rejections >= MAX_FINAL_REJECTIONS:
                            result.status, result.summary = "failed", f"{summary} — not verified: {check.reason}"
                            return
                        journal.append(f"Step {number}: finish(success=True) -> {outcome}")
                        last_action = "finish"
                        continue
                    record(summary, True, check={"claim": "finish", "verified": True, "reason": check.reason})
                else:
                    record(summary, True)
                result.status, result.summary = ("success" if success else "failed"), summary
                return

            if decision.tool == "checkpoint_done" and reliable:
                outcome, check = self._checkpoint(task, plan, decision, journal, listing, jpeg, result)
                record(outcome, check["verified"], check=check)
                journal.append(f"Step {number}: {_describe_call(decision)} -> {outcome}")
                last_action = decision.tool
                continue

            if decision.tool == "replan" and reliable:
                reason = str(decision.input.get("reason", ""))
                journal.append(f"Step {number}: replan ({reason})")
                plan = self._plan(task, apps, journal, listing, jpeg, result)
                record("new plan:\n" + render_plan(plan), True)
                last_action = decision.tool
                continue

            ok, outcome = self._execute(decision, elements)
            record(outcome, ok)
            journal.append(f"Step {number}: {_describe_call(decision)} -> {outcome}")
            last_action = decision.tool

        result.status = "max_steps"
        result.summary = f"Stopped after {self.max_steps} steps without finishing."

    def _plan(self, task: str, apps: str, journal: list[str], listing: str, jpeg: bytes,
              result: RunResult) -> list[Checkpoint]:
        context = task if not journal else f"{task}\n\nProgress so far:\n" + "\n".join(journal[-20:])
        plan, notes, usage = make_plan(self.llm, context, apps, listing, jpeg)
        result.usage.add(usage)
        result.plan = plan_to_dicts(plan)
        if self.on_plan:
            self.on_plan(plan, notes)
        return plan

    def _checkpoint(self, task: str, plan: list[Checkpoint], decision: Decision, journal: list[str],
                    listing: str, jpeg: bytes, result: RunResult) -> tuple[str, dict]:
        try:
            index = int(decision.input["index"])
            checkpoint = plan[index]
        except (KeyError, ValueError, TypeError, IndexError):
            return "error: no such checkpoint", {"claim": "checkpoint", "verified": False, "reason": "bad index"}
        claim = (f"Checkpoint {index} is reached: {checkpoint.goal} (expected evidence: {checkpoint.evidence}). "
                 f"Agent's evidence: {decision.input.get('evidence', '')}")
        check = verify(self.llm, task, claim, journal, listing, jpeg)
        result.usage.add(check.usage)
        if check.verified:
            checkpoint.done = True
            result.plan = plan_to_dicts(plan)
        verdict = "verified" if check.verified else "NOT verified"
        return (f"checkpoint {index} {verdict}: {check.reason}",
                {"claim": f"checkpoint {index}", "verified": check.verified, "reason": check.reason})

    def _observe(self) -> tuple[str, list[Element], str, bytes]:
        client = self.device.client
        tree = client.tree()
        if tree.get("locked"):
            raise DeviceError(LOCKED_MESSAGE)
        screen = client.screen()
        elements = flatten(tree, screen["width"], screen["height"])
        jpeg = observe.prepare(client.screenshot(fast=True), screen["width"], screen["height"], elements)
        return tree.get("bundleId", "?"), elements, render(elements), jpeg

    def _apps_block(self) -> str:
        try:
            apps = list_apps(self.device.target)
        except GhosthandError:
            return "Installed apps: unknown."
        lines = [f"{bundle_id}  {info.get('CFBundleDisplayName') or info.get('CFBundleName') or ''}".rstrip()
                 for bundle_id, info in sorted(apps.items())]
        return "Installed apps (bundle ID, name):\n" + "\n".join(lines)

    def _execute(self, decision: Decision, elements: list[Element]) -> tuple[bool, str]:
        args, client = decision.input, self.device.client
        try:
            match decision.tool:
                case "tap_element" | "long_press_element":
                    index = int(args["index"])
                    if not 0 <= index < len(elements):
                        return False, f"error: no element [{index}] on this screen"
                    hold = min(max(float(args.get("seconds", 1.0)), 0.3), 5.0) \
                        if decision.tool == "long_press_element" else None
                    target = elements[index]
                    current = relocate(target, self.device.elements())
                    if current is None:
                        return False, f"error: {target.describe()} is no longer on screen; nothing was tapped"
                    client.tap(*current.center, hold)
                    outcome = f"ok ({target.describe()})"
                    if moved(target, current):
                        outcome += " (it had moved; tapped its new position)"
                case "tap_point":
                    x, y = float(args["x"]), float(args["y"])
                    before = element_at(elements, x, y)
                    after = element_at(self.device.elements(), x, y)
                    if point_changed(before, after):
                        now = after.describe() if after else "nothing listed"
                        return False, (f"error: the screen changed at ({x:.0f}, {y:.0f}): it was {before.describe()}, "
                                       f"now {now}; nothing was tapped")
                    client.tap(x, y)
                    outcome = "ok"
                case "swipe":
                    self.device.swipe_direction(args["direction"])
                    outcome = "ok"
                case "swipe_points":
                    client.swipe(float(args["from_x"]), float(args["from_y"]), float(args["to_x"]), float(args["to_y"]))
                    outcome = "ok"
                case "type_text":
                    client.type_text(args["text"] + ("\n" if args.get("press_enter") else ""))
                    outcome = "ok"
                case "press_home":
                    client.press("home")
                    outcome = "ok"
                case "launch_app":
                    state = client.launch(args["bundle_id"])["state"]
                    outcome = f"ok ({state})"
                case "open_url":
                    client.open_url(args["url"])
                    time.sleep(1.5)
                    outcome = "ok"
                case "wait":
                    time.sleep(min(max(float(args.get("seconds", 1.0)), 0.5), 5.0))
                    return True, "waited"
                case _:
                    return False, f"error: unknown tool {decision.tool!r}"
        except (KeyError, ValueError, TypeError) as error:
            return False, f"error: bad arguments ({error})"
        except GhosthandError as error:
            return False, f"error: {error}"
        time.sleep(SETTLE_SECONDS)
        return True, outcome

    def _record(self, trace: Trace, result: RunResult, number: int, app: str, decision: Decision,
                outcome: str, ok: bool, screenshot: str, started: float,
                check: dict | None = None) -> Step:
        step = Step(
            number=number, app=app, action=decision.tool, input=decision.input,
            reason=str(decision.input.get("reason", "")) or decision.text.strip(),
            outcome=outcome, ok=ok, screenshot=screenshot,
            seconds=round(time.monotonic() - started, 2), usage=asdict(decision.usage),
            check=check,
        )
        result.steps.append(step)
        trace.add_step(step)
        if self.on_step:
            self.on_step(step)
        return step


def _describe_call(decision: Decision) -> str:
    args = {k: v for k, v in decision.input.items() if k != "reason"}
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{decision.tool}({rendered})"


def _prompt(task: str, journal: list[str], app: str, listing: str, number: int, max_steps: int,
            plan: list[Checkpoint] | None = None) -> str:
    history = "\n".join(journal[-40:]) or "(no steps yet)"
    plan_block = f"<plan>\n{render_plan(plan)}\n</plan>\n\n" if plan else ""
    return (f"<task>\n{task}\n</task>\n\n{plan_block}"
            f"<journal>\n{history}\n</journal>\n\n"
            f"<screen app=\"{app}\" step=\"{number} of {max_steps}\">\n{listing}\n</screen>")
