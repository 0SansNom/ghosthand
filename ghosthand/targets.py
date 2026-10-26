import os

from . import physical, simctl
from .errors import GhosthandError
from .physical import PhysicalDevice
from .simctl import Simulator

Target = Simulator | PhysicalDevice


class TargetError(GhosthandError):
    pass


def list_targets() -> list[Target]:
    return [*simctl.list_simulators(), *physical.list_devices()]


def resolve_target(selector: str | None = None) -> Target:
    # A booted simulator wins over an iPhone: a paired iPhone on Wi-Fi is "reachable" almost all the time.
    selector = selector or os.environ.get("GHOSTHAND_DEVICE")
    targets = list_targets()
    if selector:
        matches = [t for t in targets if t.udid == selector] or [
            t for t in targets if t.name.lower() == selector.lower()
        ]
        if not matches:
            raise TargetError(f"no simulator or iPhone matches {selector!r}; see `ghosthand devices`")
        return sorted(matches, key=lambda t: (t.booted, t.runtime), reverse=True)[0]

    for kind, label in (("simulator", "simulators are booted"), ("device", "iPhones are reachable")):
        ready = [t for t in targets if t.kind == kind and t.booted]
        if len(ready) == 1:
            return ready[0]
        if len(ready) > 1:
            names = ", ".join(f"{t.name} ({t.udid})" for t in ready)
            raise TargetError(f"several {label} ({names}); pass --device")
    raise TargetError("no booted simulator or reachable iPhone; run `ghosthand boot \"iPhone 17\"`, "
                      "or connect an iPhone, or pass --device")


def list_apps(target: Target) -> dict[str, dict]:
    if target.kind == "simulator":
        return simctl.list_apps(target.udid)
    return physical.list_apps(target.udid)
