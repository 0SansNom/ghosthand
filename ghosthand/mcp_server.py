import logging
import time
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import observe, runner, simctl
from .errors import GhosthandError
from .device import LOCKED_MESSAGE, Device
from .targets import list_apps, list_targets, resolve_target
from .targeting import element_at, moved, point_changed, relocate
from .tasks import Task, TaskManager
from .tree import Element, flatten, render

INSTRUCTIONS = """\
Drive an iOS Simulator or a real iPhone like a person would.

Loop: call ios_observe, decide, act (ios_tap, ios_swipe, ios_type, ios_press, ios_launch), repeat.
- Tap elements by their [index] from the latest ios_observe; indexes change whenever the screen does.
- Coordinates are in points from the top-left; the screenshot is already scaled to points, so a pixel
  position in it is a valid tap position. Use coordinates only for things missing from the element list.
- Actions return the refreshed element list. Call ios_observe again when you need to see the screen
  (images, layout, unlabeled controls) or after animations and loading.
- To type, tap the text field first so it has keyboard focus.
- iOS has no back button: use the "Back"/"Retour" button at the top left, or swipe right from the left edge.
- Open apps with ios_launch (bundle IDs from ios_apps), e.g. com.apple.Preferences for Settings.
- Never confirm purchases, deletions, payments or messages to real people unless the user asked for it.
"""

server = MCPServer("ghosthand", instructions=INSTRUCTIONS)

# Elements from each device's latest observation: [index] in a tap refers to what the model last saw.
_last_elements: dict[str, list[Element]] = {}

SETTLE_SECONDS = 0.6
READ_ONLY = ToolAnnotations(readOnlyHint=True)


def _device(selector: str | None) -> Device:
    try:
        return Device.connect(selector, start_runner=True)
    except GhosthandError as error:
        raise ToolError(str(error)) from error


def _call(action):
    try:
        return action()
    except GhosthandError as error:
        raise ToolError(str(error)) from error


def _snapshot(dev: Device) -> tuple[str, list[Element]]:
    tree = _call(lambda: dev.client.tree())
    screen = _call(dev.client.screen)
    elements = flatten(tree, screen["width"], screen["height"])
    _last_elements[dev.target.udid] = elements
    orientation = screen["orientation"]
    if orientation == "unknown":  # XCUIDevice often doesn't report it
        orientation = "portrait" if screen["height"] >= screen["width"] else "landscape"
    header = (f"App: {tree.get('bundleId', '?')} | screen {screen['width']:.0f}x{screen['height']:.0f} pt, "
              f"{orientation} | device: {dev.target.name}")
    if tree.get("locked"):
        header += f"\nWARNING: {LOCKED_MESSAGE} Ask the user to unlock it."
    return f"{header}\n{render(elements)}", elements


def _after_action(dev: Device, done: str) -> str:
    time.sleep(SETTLE_SECONDS)
    listing, _ = _snapshot(dev)
    return f"{done}\n\nScreen now:\n{listing}"


@server.tool(annotations=READ_ONLY, structured_output=False)
def ios_devices() -> str:
    """List simulators and real iPhones, and which ones are ready (booted / reachable)."""
    targets = _call(list_targets)
    lines = [f"{'READY' if t.booted else 'off  '}  {t.kind:<9} {t.name} ({t.runtime}) {t.udid}"
             for t in sorted(targets, key=lambda t: (not t.booted, t.kind, t.name))]
    return "\n".join(lines) or "No simulators. Install one with `xcodebuild -downloadPlatform iOS`."


@server.tool(structured_output=False)
def ios_boot(device: str) -> str:
    """Get a target ready: boot a simulator (name like "iPhone 17", or UDID) or pick an iPhone, then start
    the automation runner on it.

    The first start compiles the runner (a minute or two). On an iPhone it must be unlocked, and the very
    first time the user must trust the developer certificate in Settings > General > VPN & Device Management.
    """
    target = _call(lambda: resolve_target(device))
    if target.kind == "simulator":
        _call(lambda: simctl.boot(target.udid))
    state = _call(lambda: runner.start(target))
    return f"{target.name} ready, runner at {state.base_url}."


@server.tool(annotations=READ_ONLY, structured_output=False)
def ios_observe(screenshot: bool = True, annotate: bool = True, device: str | None = None) -> list:
    """See the screen: numbered list of visible elements, plus a screenshot.

    Args:
        screenshot: Include a screenshot (scaled to points, so pixel = tap coordinate).
        annotate: Draw element indexes on the screenshot.
        device: Simulator name or UDID; defaults to the only booted one.
    """
    dev = _device(device)
    listing, elements = _snapshot(dev)
    if not screenshot:
        return [listing]
    screen = _call(dev.client.screen)
    png = _call(lambda: dev.client.screenshot(fast=True))
    jpeg = observe.prepare(png, screen["width"], screen["height"], elements if annotate else None)
    return [listing, Image(data=jpeg, format="jpeg")]


@server.tool(structured_output=False)
def ios_tap(index: int | None = None, x: float | None = None, y: float | None = None,
            hold_seconds: float = 0.0, device: str | None = None) -> str:
    """Tap an element by [index] from the latest observation, or a point (x, y) in points.

    Args:
        index: Element index from the latest ios_observe or action result.
        x: Horizontal position in points (with y, instead of index).
        y: Vertical position in points.
        hold_seconds: Press duration for a long press (e.g. 1.0); 0 for a normal tap.
        device: Simulator name or UDID.
    """
    dev = _device(device)
    hold = hold_seconds or None
    elements = _last_elements.get(dev.target.udid) or _snapshot(dev)[1]
    if index is not None:
        if not 0 <= index < len(elements):
            raise ToolError(f"No element [{index}] in the latest observation ({len(elements)} elements). "
                            "Call ios_observe.")
        element = elements[index]
        current = relocate(element, _call(dev.elements))
        if current is None:
            raise ToolError(f"{element.describe()} is no longer on screen; nothing was tapped. Call ios_observe.")
        _call(lambda: dev.client.tap(*current.center, hold))
        note = " (it had moved; tapped its new position)" if moved(element, current) else ""
        return _after_action(dev, f"Tapped {element.describe()}{note}")
    if x is None or y is None:
        raise ToolError("Give either index, or both x and y.")
    before, after = element_at(elements, x, y), element_at(_call(dev.elements), x, y)
    if point_changed(before, after):
        now = after.describe() if after else "nothing listed"
        raise ToolError(f"The screen changed at ({x:.0f}, {y:.0f}): it was {before.describe()}, now {now}; "
                        "nothing was tapped. Call ios_observe.")
    _call(lambda: dev.client.tap(x, y, hold))
    return _after_action(dev, f"Tapped ({x:.0f}, {y:.0f})")


@server.tool(structured_output=False)
def ios_swipe(direction: Literal["up", "down", "left", "right"] | None = None,
              from_x: float | None = None, from_y: float | None = None,
              to_x: float | None = None, to_y: float | None = None,
              duration: float = 0.3, device: str | None = None) -> str:
    """Swipe: either a direction from the screen center, or explicit points.

    The direction is the finger's: "up" scrolls content down (reveals what is below).
    To go back in most apps, swipe from x=2 to x=250 at mid-height.

    Args:
        direction: up, down, left or right.
        from_x: Start x in points (with from_y, to_x, to_y instead of direction).
        from_y: Start y in points.
        to_x: End x in points.
        to_y: End y in points.
        duration: Gesture duration in seconds; shorter is a faster fling.
        device: Simulator name or UDID.
    """
    dev = _device(device)
    if direction:
        _call(lambda: dev.swipe_direction(direction))
        return _after_action(dev, f"Swiped {direction}")
    if None in (from_x, from_y, to_x, to_y):
        raise ToolError("Give a direction, or all of from_x, from_y, to_x, to_y.")
    _call(lambda: dev.client.swipe(from_x, from_y, to_x, to_y, duration))
    return _after_action(dev, f"Swiped ({from_x:.0f},{from_y:.0f}) -> ({to_x:.0f},{to_y:.0f})")


@server.tool(structured_output=False)
def ios_type(text: str, device: str | None = None) -> str:
    """Type text into the field that has keyboard focus (tap the field first). Use "\\n" for Return."""
    dev = _device(device)
    _call(lambda: dev.client.type_text(text))
    return _after_action(dev, f"Typed {text!r}")


@server.tool(structured_output=False)
def ios_press(button: Literal["home"], device: str | None = None) -> str:
    """Press a hardware button. "home" goes to the home screen."""
    dev = _device(device)
    _call(lambda: dev.client.press(button))
    return _after_action(dev, f"Pressed {button}")


@server.tool(structured_output=False)
def ios_launch(bundle_id: str, relaunch: bool = False, device: str | None = None) -> str:
    """Open an app by bundle ID (see ios_apps). relaunch=True restarts it from scratch."""
    dev = _device(device)
    result = _call(lambda: dev.client.launch(bundle_id, relaunch))
    return _after_action(dev, f"Launched {bundle_id} ({result['state']})")


@server.tool(structured_output=False)
def ios_terminate(bundle_id: str, device: str | None = None) -> str:
    """Quit an app."""
    dev = _device(device)
    _call(lambda: dev.client.terminate(bundle_id))
    return _after_action(dev, f"Terminated {bundle_id}")


@server.tool(annotations=READ_ONLY, structured_output=False)
def ios_apps(device: str | None = None) -> str:
    """List installed apps: bundle ID and display name."""
    target = _call(lambda: resolve_target(device))
    installed = _call(lambda: list_apps(target))
    return "\n".join(
        f"{bundle_id}  {info.get('CFBundleDisplayName') or info.get('CFBundleName') or ''}".rstrip()
        for bundle_id, info in sorted(installed.items())
    )


@server.tool(structured_output=False)
def ios_open_url(url: str, device: str | None = None) -> str:
    """Open a URL or deep link (https://…, maps://…, app schemes)."""
    dev = _device(device)
    before = _call(dev.client.health)["currentApp"]
    _call(lambda: dev.client.open_url(url))
    # The app switch animates for a second or more; wait for it rather than guess.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _call(dev.client.health)["currentApp"] == before:
        time.sleep(0.3)
    return _after_action(dev, f"Opened {url}")


_tasks = TaskManager()


def _task_report(task: Task, last_steps: int = 5) -> str:
    lines = [f"Task {task.run_id} on {task.device}: {task.status}", f"Goal: {task.task}"]
    if task.result:
        lines.append(f"Result: {task.result.summary}")
        lines.append(f"{len(task.result.steps)} steps in {task.result.seconds}s, trace: {task.result.trace_dir}")
    elif task.error:
        lines.append(f"Error: {task.error}")
    if task.steps:
        lines.append(f"Last steps ({len(task.steps)} so far):")
        for step in task.steps[-last_steps:]:
            lines.append(f"  {step.number}. {step.action} -> {step.outcome} | {step.reason}")
    return "\n".join(lines)


@server.tool(structured_output=False)
def ios_run_task(task: str, max_steps: int = 30, wait_seconds: float = 0,
                 mode: Literal["fast", "reliable"] = "fast", device: str | None = None) -> str:
    """Hand a whole task to the built-in agent (Claude via the Anthropic API) instead of driving step by step.

    It runs in the background; poll with ios_task_status. Needs ANTHROPIC_API_KEY (or `ant auth login`)
    in the server's environment.

    Args:
        task: What to do, in plain language, e.g. "Turn on dark mode in Settings".
        max_steps: Step budget before giving up.
        wait_seconds: Block up to this long for the result (0 returns immediately).
        mode: "fast" acts directly; "reliable" plans checkpoints and has each one verified (slower, surer).
        device: Simulator name or UDID.
    """
    dev = _device(device)
    try:
        entry = _tasks.start(task, dev, max_steps=max_steps, mode=mode)
    except (RuntimeError, GhosthandError) as error:
        raise ToolError(str(error)) from error
    if wait_seconds > 0 and entry.thread:
        entry.thread.join(timeout=min(wait_seconds, 600))
    return _task_report(entry)


@server.tool(annotations=READ_ONLY, structured_output=False)
def ios_task_status(task_id: str | None = None) -> str:
    """Status and latest steps of an agent task; without task_id, a list of all tasks."""
    if task_id is None:
        tasks = _tasks.all()
        return "\n".join(f"{t.run_id}  {t.status:<10} {t.task}" for t in tasks) or "No tasks yet."
    entry = _tasks.get(task_id)
    if entry is None:
        raise ToolError(f"Unknown task {task_id!r}.")
    return _task_report(entry, last_steps=10)


@server.tool(structured_output=False)
def ios_task_cancel(task_id: str) -> str:
    """Stop an agent task after its current step."""
    entry = _tasks.get(task_id)
    if entry is None:
        raise ToolError(f"Unknown task {task_id!r}.")
    entry.cancel.set()
    return f"Cancelling {task_id}; it stops after the current step."


def serve() -> None:
    # One INFO line per runner request would drown the server's own logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")
