# ghosthand

Natural-language automation for iOS: an agent (Claude) drives an iOS Simulator or a real iPhone the way a person would.

- **`Runner/`** (Swift): an XCUITest app that runs on the device and serves a small HTTP API: accessibility tree, screenshots, taps, swipes, typing, buttons, app launching.
- **`ghosthand/`** (Python): drives simulators (`simctl`) and iPhones (`devicectl`), starts and talks to the runner, exposes everything as an **MCP server**, and includes an **autonomous agent** (Claude via the Anthropic API) and a **web console**.
- **`packages/ghosthand-client/`**: zero-dependency Python SDK to drive the console from pytest or CI.

## Requirements

- macOS with Xcode (tested with Xcode 27.1) and an iOS Simulator runtime (`xcodebuild -downloadPlatform iOS`)
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)

## Getting started

```bash
uv sync
uv run ghosthand boot "iPhone 17"     # boot a simulator
uv run ghosthand runner start         # build (first time) and start the runner
uv run ghosthand tree                 # visible elements, numbered
uv run ghosthand tap -i 4             # tap element [4]
uv run ghosthand launch com.apple.Preferences
uv run ghosthand swipe up
uv run ghosthand type "hello"
uv run ghosthand screenshot screen.png
uv run ghosthand doctor
```

Default target: the only booted simulator, else the only reachable iPhone. Otherwise pick one with `--device "iPhone 17"` or `GHOSTHAND_DEVICE`.
The runner is rebuilt automatically when its Swift sources change. Its logs are in `~/.ghosthand/runners/`.

## On a real iPhone

One-time setup:

1. **Xcode > Settings > Accounts**: add your Apple ID. A free account works ("Personal Team").
2. **iPhone**: connect it over USB and accept "Trust This Computer". Afterwards, Wi-Fi works too.
3. **Developer Mode**: Settings > Privacy & Security > Developer Mode (requires a restart).
4. **Auto-Lock**: Settings > Display & Brightness > Auto-Lock > **Never** while automating.
   A locked iPhone cannot be driven; the agent stops with a clear message.

```bash
uv run ghosthand devices                          # the iPhone must be "reachable"
uv run ghosthand runner start -d "My iPhone"      # builds, signs, installs and starts the runner
```

On the very first launch, iOS refuses apps from an unknown developer: on the iPhone, go to **Settings > General >
VPN & Device Management > Apple Development: <your email> > Trust**, then start again.
The CLI prints these instructions when it detects the problem.

How it works:
- the runner is signed with your team (set `GHOSTHAND_TEAM` to force one) and bundle IDs unique to your account;
- with a free account, signing expires after **7 days**: the runner is rebuilt automatically after 6 days;
- the Mac reaches the runner through the CoreDevice tunnel that Xcode opens to the iPhone (USB or Wi-Fi). Its IPv6
  address is resolved once at startup, because each mDNS lookup of `<udid>.coredevice.local` takes several seconds;
- screenshots are downscaled to point size and JPEG-encoded on the iPhone: about 30 KB instead of 1.7 MB.

## With Claude Code (MCP)

```bash
uv run ghosthand mcp install            # = claude mcp add --scope user ghosthand -- uv --directory <here> run ghosthand mcp serve
```

Restart Claude Code, then ask for example: *"On the simulator, open Settings and turn on dark mode."*
The runner starts on the first call. No API key is needed: Claude Code does the thinking.

| Tool | Purpose |
|---|---|
| `ios_observe` | numbered element list + annotated screenshot (scaled to points) |
| `ios_tap` | tap by `index` or `x`/`y`, long press with `hold_seconds` |
| `ios_swipe` | a direction, or start and end points |
| `ios_type` | type into the focused field |
| `ios_press` | Home button |
| `ios_launch` / `ios_terminate` | open / quit an app |
| `ios_open_url` | URL or deep link |
| `ios_apps` | installed apps |
| `ios_devices` / `ios_boot` | list targets / get one ready (and start its runner) |
| `ios_run_task` / `ios_task_status` / `ios_task_cancel` | hand a whole task to the built-in agent |

Each action returns the refreshed element list, saving a round trip.
An `index` always refers to the **latest** observation returned to the model.

## Autonomous agent

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # or `ant auth login`
uv run ghosthand run "Turn on dark mode in Settings"
uv run ghosthand traces                  # recent runs
```

Each step is **one independent request** to the model: the task, a text journal of previous steps and the current
screen (numbered elements + annotated screenshot). The model answers with **exactly one action**
(`tap_element`, `tap_point`, `long_press_element`, `swipe`, `swipe_points`, `type_text`, `press_home`,
`launch_app`, `open_url`, `wait` or `finish`) and a one-sentence reason.

- Old screenshots are never resent: only the text journal grows, so the cost per step stays flat.
- An action with no visible effect is reported to the model ("the screen did not change"), and so are errors.
- Stops on `finish`, the step budget (`--max-steps`, default 30), a model refusal, or 3 answers without an action.
- Model: `claude-opus-5-5` at `low` effort by default (`--model`, `--effort`, or `GHOSTHAND_MODEL` /
  `GHOSTHAND_EFFORT`). Server-side refusal fallbacks (`fallbacks: "default"`) are enabled.

### Reliable mode

```bash
uv run ghosthand run "Turn on dark mode in Settings" --mode reliable
```

- **Plan**: before acting, one request produces 2 to 6 checkpoints, each with the evidence expected on screen.
- **Verification**: the agent claims a checkpoint with `checkpoint_done`; an independent reviewer, which took no
  action and sees only the screen and the journal, confirms or rejects it. `finish(success=true)` is reviewed the same
  way: after two rejections the task fails instead of reporting a false success.
- **Replanning**: `replan` when the situation no longer matches the plan.
- Slower and more expensive than `fast` (one extra request per checkpoint), but a reported success is a seen success.

There is no risk guard: the prompts tell the model not to complete purchases, deletions or
messages unless the task asks for them, and nothing else enforces it. Supervise runs on a real iPhone.

### Target check (before every tap)

The model decides from a screen captured seconds earlier; animations, loading or scrolling can move or replace
its target in the meantime. Right before tapping, the screen is read again:
- an element tapped by index is found again by type, label and identifier: tapped where it is now if it moved,
  not tapped at all (and reported to the model) if it is gone;
- a point tap aimed at a labeled element is cancelled if a different element is now under that point.

The same check applies to `ios_tap` in the MCP server.

**Traces**: `~/.ghosthand/traces/<id>/` holds `run.json` (task, mode, plan, status, summary, duration, tokens),
`steps.jsonl` (one line per step, with checks) and `step-NNN.jpg` (the screen the model saw).

From an MCP client: `ios_run_task` starts the agent in the background, `ios_task_status` follows it and
`ios_task_cancel` stops it. The API key must then be in the MCP server's environment
(`claude mcp add … -e ANTHROPIC_API_KEY=…`).

## Web console

```bash
uv run ghosthand ui            # http://localhost:8000, opens the browser
```

- **Live screen** of the simulator or iPhone: click to tap, hold for a long press, drag to swipe; Home, typing,
  opening an app or a URL; starting and stopping the runner.
- **Agent**: run a task (fast or reliable mode, step budget) and follow the plan and steps live.
- **Traces**: replay any run step by step (screen the agent saw, reason, outcome, checks, plan);
  ← → keys to navigate.
- **Elements**: the numbered list exactly as the model sees it.

The console listens on `127.0.0.1` only and refuses requests from other hosts or other sites (DNS rebinding, CSRF),
since it controls your phone.

API (used by the page and the SDK): `GET /api/devices`, `POST /api/devices/{udid}/runner`,
`GET /api/devices/{udid}/screen.jpg`, `GET /api/devices/{udid}/elements`, `POST /api/devices/{udid}/action`,
`GET|POST /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks/{id}/cancel`,
`GET /api/traces`, `GET /api/traces/{id}`, `GET /api/traces/{id}/step-NNN.jpg`.

## Client SDK (tests, CI)

```python
from ghosthand_client import GhosthandClient

def test_dark_mode():
    result = GhosthandClient().run("Turn on dark mode in Settings", mode="reliable")
    assert result.succeeded, result.summary
```

See `packages/ghosthand-client/README.md`.

## Tests

```bash
uv run pytest        # no device or API key needed: fake model and fake runner
```

## Runner API

Every request must send `Authorization: Bearer <token>`. `runner start` creates the token and stores it in `~/.ghosthand/runners/`. On the simulator the listener also accepts only connections from this Mac; an iPhone is reached through the CoreDevice tunnel, with the same token.

| Route | Purpose |
|---|---|
| `GET /health` | status, current app, whether the iPhone is locked |
| `GET /screen` | size in points, scale, orientation |
| `GET /screenshot?format=jpeg` | full-resolution PNG, or a point-sized JPEG |
| `GET /tree?bundleId=&maxDepth=` | raw accessibility tree of the foreground app (+ system alerts) |
| `POST /tap` `{x, y, duration?}` | tap or long press, in points |
| `POST /swipe` `{fromX, fromY, toX, toY, duration?}` | swipe |
| `POST /type` `{text}` | type into the focused field |
| `POST /press` `{button}` | `home` (`volumeUp`/`volumeDown` on an iPhone) |
| `POST /launch` `{bundleId, relaunch?}` | open an app |
| `POST /terminate` `{bundleId}` | quit an app |
| `POST /app-state` `{bundleIds}` | state of each app |
| `POST /known-apps` `{bundleIds}` | installed apps, used to find the foreground one (sent at startup) |
| `POST /open-url` `{url}` | open a URL or deep link |
| `POST /shutdown` | stop the runner |

## Editing the Xcode project

`Runner/GhosthandRunner.xcodeproj` is generated from `Runner/project.yml` with [XcodeGen](https://github.com/yonaskolb/XcodeGen): `cd Runner && xcodegen generate`.
