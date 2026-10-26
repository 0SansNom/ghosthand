import math

from .tree import Element

MOVE_TOLERANCE = 4.0


def element_at(elements: list[Element], x: float, y: float) -> Element | None:
    """The smallest listed element containing the point."""
    hits = [e for e in elements if e.x <= x <= e.x + e.width and e.y <= y <= e.y + e.height]
    return min(hits, key=lambda e: e.width * e.height, default=None)


def same_identity(a: Element, b: Element) -> bool:
    return a.type == b.type and a.label == b.label and a.identifier == b.identifier


def distance(a: Element, b: Element) -> float:
    (ax, ay), (bx, by) = a.center, b.center
    return math.hypot(ax - bx, ay - by)


def relocate(target: Element, fresh: list[Element]) -> Element | None:
    """Where `target` is on the current screen, or None if it is gone.

    The model decides from a screen captured seconds earlier; animations, loading and scrolling
    can move or replace what it picked in the meantime.
    """
    candidates = [e for e in fresh if same_identity(e, target)]
    if not target.label and not target.identifier:
        # Nothing but the position identifies an unlabeled element, so it must not have moved far.
        candidates = [e for e in candidates if distance(e, target) <= max(target.height, MOVE_TOLERANCE)]
    return min(candidates, key=lambda e: distance(e, target), default=None)


def moved(before: Element, after: Element) -> bool:
    return distance(before, after) > MOVE_TOLERANCE


def point_changed(before: Element | None, after: Element | None) -> bool:
    """A point tap aimed at a labeled element that is no longer the one under the point."""
    if before is None or not (before.label or before.identifier):
        return False
    return after is None or not same_identity(before, after)
