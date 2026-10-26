import json
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import physical, runner, simctl
from .device import Device
from .errors import GhosthandError
from .paths import PROJECT_ROOT, RUNNER_PROJECT
from .targets import list_apps, list_targets, resolve_target
from .tree import render

app = typer.Typer(help="Drive an iOS Simulator or an iPhone through the Ghosthand runner.", no_args_is_help=True)
runner_app = typer.Typer(help="Manage the XCUITest runner on the device.", no_args_is_help=True)
app.add_typer(runner_app, name="runner")
mcp_app = typer.Typer(help="MCP server for Claude Code and other clients.", no_args_is_help=True)
app.add_typer(mcp_app, name="mcp")

DeviceOption = Annotated[
    Optional[str],
    typer.Option("--device", "-d", help="Simulator or iPhone name or UDID "
                                        "(default: the only booted simulator, else the only reachable iPhone)."),
]


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _hint(message: str) -> None:
    typer.secho(f"→ {message}", fg=typer.colors.YELLOW)


def _device(selector: str | None) -> Device:
    try:
        return Device.connect(selector, start_runner=True, on_wait=_hint)
    except GhosthandError as error:
        _fail(str(error))


def _guard(action):
    try:
        return action()
    except GhosthandError as error:
        _fail(str(error))


@app.command()
def devices() -> None:
    """List simulators and iPhones."""
    for target in sorted(_guard(list_targets), key=lambda t: (t.kind, not t.booted, t.runtime, t.name)):
        if target.kind == "simulator":
            marker = typer.style("● booted   ", fg=typer.colors.GREEN) if target.booted else "  shutdown "
        else:
            marker = typer.style("● reachable", fg=typer.colors.GREEN) if target.booted else "  offline  "
        kind = "iPhone" if target.kind == "device" else "sim"
        typer.echo(f"{marker} {kind:<6} {target.name:<24} {target.runtime:<10} {target.udid}")


@app.command()
def boot(device: Annotated[str, typer.Argument(help="Simulator name or UDID.")]) -> None:
    """Boot a simulator and open the Simulator app."""
    target = _guard(lambda: resolve_target(device))
    if target.kind != "simulator":
        _fail(f"{target.name} is a real iPhone: nothing to boot, run `ghosthand runner start -d \"{target.name}\"`.")
    _guard(lambda: simctl.boot(target.udid))
    typer.echo(f"{target.name} booted ({target.udid})")


@runner_app.command("build")
def runner_build(device: DeviceOption = None) -> None:
    """Build (and sign, for an iPhone) the runner."""
    target = _guard(lambda: resolve_target(device))
    typer.echo(f"Building the runner for {target.name}… (1-2 minutes the first time)")
    path = _guard(lambda: runner.build(target))
    typer.echo(f"OK: {path}")


@runner_app.command("start")
def runner_start(device: DeviceOption = None) -> None:
    """Start the runner on the device, building it if needed."""
    target = _guard(lambda: resolve_target(device))
    typer.echo(f"Starting the runner on {target.name}…")
    state = _guard(lambda: runner.start(target, on_wait=_hint))
    typer.echo(f"Runner ready at {state.base_url} (log: {state.log})")


@runner_app.command("stop")
def runner_stop(device: DeviceOption = None) -> None:
    """Stop the runner."""
    target = _guard(lambda: resolve_target(device))
    typer.echo("Runner stopped." if runner.stop(target.udid) else "No runner running.")


@runner_app.command("status")
def runner_status(device: DeviceOption = None) -> None:
    """Check that the runner answers."""
    target = _guard(lambda: resolve_target(device))
    state = runner.load_state(target.udid)
    if state is None:
        _fail("No runner started.")
    health = _guard(runner.client_for(target.udid, timeout=5.0).health)
    typer.echo(f"Runner OK at {state.base_url}, current app: {health['currentApp']}")


@app.command()
def tree(
    device: DeviceOption = None,
    raw: Annotated[bool, typer.Option(help="Print the raw tree as JSON.")] = False,
    bundle_id: Annotated[Optional[str], typer.Option(help="App to inspect (default: foreground app).")] = None,
) -> None:
    """Print the visible elements, numbered."""
    dev = _device(device)
    if _guard(dev.client.health).get("locked"):
        _hint("The iPhone is locked: unlock it, and set Display & Brightness > Auto-Lock to Never.")
    if raw:
        typer.echo(json.dumps(_guard(lambda: dev.client.tree(bundle_id)), indent=2, ensure_ascii=False))
    else:
        typer.echo(render(_guard(lambda: dev.elements(bundle_id))))


@app.command()
def screenshot(
    output: Annotated[Path, typer.Argument(help="Output PNG file (full resolution).")] = Path("screenshot.png"),
    device: DeviceOption = None,
) -> None:
    """Save a screenshot."""
    dev = _device(device)
    output.write_bytes(_guard(dev.client.screenshot))
    typer.echo(f"Saved {output}")


@app.command()
def tap(
    x: Annotated[Optional[float], typer.Argument()] = None,
    y: Annotated[Optional[float], typer.Argument()] = None,
    index: Annotated[Optional[int], typer.Option("--index", "-i", help="Element number (see `tree`).")] = None,
    hold: Annotated[float, typer.Option(help="Press duration in seconds (long press).")] = 0.0,
    device: DeviceOption = None,
) -> None:
    """Tap an element (--index) or a point in points."""
    dev = _device(device)
    if index is not None:
        element = _guard(lambda: dev.tap_element(index, hold or None))
        typer.echo(f"Tapped {element.describe()}")
    elif x is not None and y is not None:
        _guard(lambda: dev.client.tap(x, y, hold or None))
        typer.echo(f"Tapped ({x:.0f}, {y:.0f})")
    else:
        _fail("Give either --index N or X Y.")


@app.command()
def swipe(
    direction: Annotated[str, typer.Argument(help="up, down, left or right (finger direction).")],
    device: DeviceOption = None,
) -> None:
    """Swipe from the screen center."""
    dev = _device(device)
    _guard(lambda: dev.swipe_direction(direction))
    typer.echo(f"Swiped {direction}")


@app.command("type")
def type_text(text: str, device: DeviceOption = None) -> None:
    """Type into the focused text field."""
    dev = _device(device)
    _guard(lambda: dev.client.type_text(text))
    typer.echo("Typed.")


@app.command()
def press(button: Annotated[str, typer.Argument(help="home (volumeUp/volumeDown on a real iPhone).")],
          device: DeviceOption = None) -> None:
    """Press a hardware button."""
    dev = _device(device)
    _guard(lambda: dev.client.press(button))
    typer.echo(f"Pressed {button}.")


@app.command()
def launch(
    bundle_id: str,
    relaunch: Annotated[bool, typer.Option(help="Restart the app if it is already running.")] = False,
    device: DeviceOption = None,
) -> None:
    """Open an app by bundle ID (e.g. com.apple.Preferences)."""
    dev = _device(device)
    result = _guard(lambda: dev.client.launch(bundle_id, relaunch))
    typer.echo(f"{bundle_id}: {result['state']}")


@app.command()
def apps(device: DeviceOption = None) -> None:
    """List installed apps."""
    target = _guard(lambda: resolve_target(device))
    installed = _guard(lambda: list_apps(target))
    for bundle_id, info in sorted(installed.items()):
        name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or ""
        typer.echo(f"{bundle_id:<45} {name}")


@app.command()
def doctor() -> None:
    """Check prerequisites."""
    checks = []
    xcode = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True)
    checks.append(("Xcode", xcode.returncode == 0, xcode.stdout.splitlines()[0] if xcode.stdout else xcode.stderr.strip()))
    try:
        sims = simctl.list_simulators()
        booted = [s.name for s in sims if s.booted]
        checks.append(("Simulators", bool(sims), f"{len(sims)} available, booted: {', '.join(booted) or 'none'}"))
    except GhosthandError as error:
        checks.append(("Simulators", False, str(error)))
    iphones = physical.list_devices()
    for phone in iphones:
        ok = phone.booted and phone.developer_mode == "enabled"
        detail = f"{phone.runtime}, {phone.transport or 'not connected'}, developer mode: {phone.developer_mode or '?'}"
        checks.append((f"iPhone {phone.name}", ok, detail))
    team = physical.signing_team()
    checks.append(("Signing team", team is not None or not iphones,
                   team or "add your Apple ID in Xcode > Settings > Accounts (needed for an iPhone)"))
    checks.append(("Runner project", RUNNER_PROJECT.exists(), str(RUNNER_PROJECT)))
    for kind in ("simulator", "device"):
        built = runner.find_xctestrun(runner.products_dir(kind))
        label = "Simulator runner" if kind == "simulator" else "iPhone runner"
        checks.append((label, built is not None, str(built) if built else "built on first `runner start`"))
    checks.append(("XcodeGen (optional)", shutil.which("xcodegen") is not None, "to edit Runner/project.yml"))

    for name, ok, detail in checks:
        mark = typer.style("✓", fg=typer.colors.GREEN) if ok else typer.style("✗", fg=typer.colors.RED)
        typer.echo(f"{mark} {name:<22} {detail}")


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="What to do, in plain language.")],
    max_steps: Annotated[int, typer.Option(help="Step budget.")] = 30,
    model: Annotated[Optional[str], typer.Option(help="Claude model (default: claude-opus-5-5).")] = None,
    effort: Annotated[Optional[str], typer.Option(help="low, medium, high… (default: low).")] = None,
    mode: Annotated[str, typer.Option(help="fast (direct) or reliable (plan + verified checkpoints).")] = "fast",
    device: DeviceOption = None,
) -> None:
    """Hand a whole task to the agent (needs ANTHROPIC_API_KEY)."""
    from .agent import Agent
    from .llm import DEFAULT_EFFORT, DEFAULT_MODEL, Claude, LLMError

    dev = _device(device)
    try:
        llm = Claude(model or DEFAULT_MODEL, effort or DEFAULT_EFFORT)
    except LLMError as error:
        _fail(str(error))

    def show(step) -> None:
        color = typer.colors.GREEN if step.ok else typer.colors.RED
        typer.secho(f"{step.number:>2}. {step.action} {_args(step.input)}", fg=color, nl=False)
        typer.echo(f"  ({step.seconds}s)")
        if step.reason:
            typer.secho(f"    {step.reason}", dim=True)
        if step.check:
            mark = "✓" if step.check["verified"] else "✗"
            typer.secho(f"    check {mark} {step.check['reason']}",
                        fg=typer.colors.GREEN if step.check["verified"] else typer.colors.YELLOW)
        elif not step.ok:
            typer.secho(f"    {step.outcome}", fg=typer.colors.RED)

    def show_plan(plan, notes: str) -> None:
        typer.secho("Plan:", bold=True)
        for index, checkpoint in enumerate(plan):
            typer.echo(f"  {index}. {checkpoint.goal}  [{checkpoint.evidence}]")
        if notes:
            typer.secho(f"  note: {notes}", dim=True)
        typer.echo()

    if mode not in ("fast", "reliable"):
        _fail("--mode must be fast or reliable.")
    typer.echo(f"Task: {task}\nDevice: {dev.target.name} | model: {llm.model} ({llm.effort}) | mode: {mode}"
               "\n")
    agent = Agent(dev, llm, max_steps=max_steps, on_step=show, on_plan=show_plan, mode=mode)
    result = agent.run(task)
    color = {"success": typer.colors.GREEN, "failed": typer.colors.YELLOW}.get(result.status, typer.colors.RED)
    typer.secho(f"\n{result.status.upper()}: {result.summary}", fg=color, bold=True)
    usage = result.usage
    typer.echo(f"{len(result.steps)} steps, {result.seconds}s, tokens: {usage.input_tokens} in "
               f"(+{usage.cache_read_tokens} cached) / {usage.output_tokens} out")
    typer.echo(f"Trace: {result.trace_dir}")
    if result.status != "success":
        raise typer.Exit(1)


def _args(arguments: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in arguments.items() if k != "reason")


@app.command()
def traces(limit: Annotated[int, typer.Option(help="Number of traces to list.")] = 15) -> None:
    """List recent agent runs."""
    from .paths import TRACES_DIR

    runs = sorted(TRACES_DIR.glob("*/run.json"), reverse=True)[:limit]
    if not runs:
        typer.echo("No traces.")
    for path in runs:
        data = json.loads(path.read_text())
        typer.echo(f"{data['run_id']}  {data.get('status', '?'):<9} {data.get('steps', '?'):>3} steps  {data['task']}")


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="HTTP port.")] = 8000,
    host: Annotated[str, typer.Option(help="Listen address (keep 127.0.0.1: the console controls your phone).")]
    = "127.0.0.1",
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the browser.")] = True,
) -> None:
    """Web console: live screen, tasks, trace replay."""
    import logging
    import threading
    import webbrowser

    import uvicorn

    from .web import create_app

    logging.getLogger("httpx").setLevel(logging.WARNING)
    url = f"http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}"
    typer.echo(f"Console at {url} (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the MCP server on stdio (started by the MCP client)."""
    from .mcp_server import serve

    serve()


@mcp_app.command("install")
def mcp_install(
    scope: Annotated[str, typer.Option(help="user (all projects), project (.mcp.json) or local.")] = "user",
) -> None:
    """Register the server in Claude Code (`claude mcp add`)."""
    if shutil.which("claude") is None:
        _fail("`claude` CLI not found. Add the server by hand: " + " ".join(_serve_command()))
    command = ["claude", "mcp", "add", "--scope", scope, "ghosthand", "--", *_serve_command()]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(result.stderr.strip() or result.stdout.strip())
    typer.echo(result.stdout.strip() or "ghosthand server added.")
    typer.echo("Restart Claude Code to load the ios_* tools.")


def _serve_command() -> list[str]:
    uv = shutil.which("uv") or "uv"
    return [uv, "--directory", str(PROJECT_ROOT), "run", "ghosthand", "mcp", "serve"]
