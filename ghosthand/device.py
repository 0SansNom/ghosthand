from dataclasses import dataclass
from typing import Callable

from . import runner
from .targets import Target, TargetError, resolve_target
from .tree import Element, flatten

DeviceError = TargetError

SWIPE_FRACTION = 0.3

LOCKED_MESSAGE = ("The iPhone is locked. Unlock it by hand (Face ID or passcode); to keep it unlocked, set "
                  "Settings > Display & Brightness > Auto-Lock to Never while automating.")


@dataclass
class Device:
    target: Target
    client: runner.RunnerClient

    @classmethod
    def connect(cls, selector: str | None = None, start_runner: bool = False,
                on_wait: Callable[[str], None] | None = None) -> "Device":
        target = resolve_target(selector)
        if not target.booted:
            if target.kind == "simulator":
                raise DeviceError(f"{target.name} is not booted; run `ghosthand boot {target.udid}`")
            raise DeviceError(f"{target.name} is not reachable: connect it by USB or put it on the same Wi-Fi")
        if start_runner:
            state = runner.start(target, on_wait=on_wait)  # no-op when already up
            return cls(target, runner.client_from(state))
        return cls(target, runner.client_for(target.udid))

    def elements(self, bundle_id: str | None = None) -> list[Element]:
        screen = self.client.screen()
        return flatten(self.client.tree(bundle_id), screen["width"], screen["height"])

    def tap_element(self, index: int, hold: float | None = None) -> Element:
        elements = self.elements()
        if not 0 <= index < len(elements):
            raise DeviceError(f"no element [{index}] on screen ({len(elements)} elements)")
        element = elements[index]
        self.client.tap(*element.center, hold)
        return element

    def swipe_direction(self, direction: str) -> None:
        # The direction is the finger's: "up" scrolls content down.
        screen = self.client.screen()
        w, h = screen["width"], screen["height"]
        cx, cy = w / 2, h / 2
        dx = {"left": -1, "right": 1}.get(direction, 0) * w * SWIPE_FRACTION
        dy = {"up": -1, "down": 1}.get(direction, 0) * h * SWIPE_FRACTION
        if not dx and not dy:
            raise DeviceError(f"unknown direction {direction!r}; expected up, down, left or right")
        self.client.swipe(cx - dx / 2, cy - dy / 2, cx + dx / 2, cy + dy / 2)
