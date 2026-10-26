from dataclasses import dataclass

INTERACTIVE = {
    "Button", "Link", "Cell", "TextField", "SecureTextField", "TextView", "SearchField",
    "Switch", "Toggle", "Slider", "Stepper", "SegmentedControl", "Picker", "PickerWheel",
    "DatePicker", "Icon", "Key", "MenuItem", "PageIndicator",
}
# Where keyboard focus means something. WebKit marks every web element as focused.
TEXT_INPUTS = {"TextField", "SecureTextField", "TextView", "SearchField"}
# Containers whose label never deserves its own line. "Other" and "Group" are
# not here: custom views (SwiftUI, Flutter…) often surface as labeled "Other".
STRUCTURAL = {
    "Application", "Window", "ScrollView", "Table", "CollectionView",
    "Toolbar", "NavigationBar", "TabBar", "StatusBar", "Keyboard",
}


@dataclass
class Element:
    index: int
    type: str
    x: float
    y: float
    width: float
    height: float
    label: str = ""
    identifier: str = ""
    value: str = ""
    placeholder: str = ""
    enabled: bool = True
    selected: bool = False
    focused: bool = False

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    def describe(self) -> str:
        parts = [f"[{self.index}] {self.type}"]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.value and self.value != self.label:
            parts.append(f'value="{self.value}"')
        if self.placeholder:
            parts.append(f'placeholder="{self.placeholder}"')
        if self.identifier and self.identifier != self.label:
            parts.append(f"id={self.identifier}")
        flags = [name for name, on in
                 (("disabled", not self.enabled), ("selected", self.selected),
                  ("focused", self.focused and self.type in TEXT_INPUTS)) if on]
        if flags:
            parts.append(f"({', '.join(flags)})")
        cx, cy = self.center
        parts.append(f"@({cx:.0f},{cy:.0f})")
        return " ".join(parts)


def _visible(frame: dict, screen_w: float, screen_h: float) -> bool:
    x, y, w, h = frame["x"], frame["y"], frame["width"], frame["height"]
    return w > 1 and h > 1 and x < screen_w and y < screen_h and x + w > 0 and y + h > 0


def flatten(tree: dict, screen_w: float, screen_h: float) -> list[Element]:
    elements: list[Element] = []
    seen: set[tuple] = set()

    def walk(node: dict, parent_label: str) -> None:
        node_type = node.get("type", "Other")
        frame = node.get("frame") or {"x": 0, "y": 0, "width": 0, "height": 0}
        label = _clean(node.get("label") or node.get("title") or "")
        value = _clean(node.get("value", ""))
        has_text = bool(label or value or node.get("placeholder"))

        keep = _visible(frame, screen_w, screen_h) and (
            node_type in INTERACTIVE or (has_text and node_type not in STRUCTURAL)
        )
        # A button or cell label usually repeats its children's text; skip the echo.
        if keep and node_type not in INTERACTIVE and label and label in parent_label:
            keep = False
        signature = (node_type, label, value, frame["x"], frame["y"], frame["width"], frame["height"])
        if keep and signature in seen:
            keep = False
        seen.add(signature)

        if keep:
            elements.append(Element(
                index=len(elements),
                type=node_type,
                x=frame["x"], y=frame["y"], width=frame["width"], height=frame["height"],
                label=label,
                identifier=node.get("identifier", ""),
                value=value,
                placeholder=_clean(node.get("placeholder", "")),
                enabled=node.get("enabled", True),
                selected=node.get("selected", False),
                focused=node.get("focused", False),
            ))
        for child in node.get("children", []):
            walk(child, label if keep else parent_label)

    walk(tree, "")
    elements = [e for e in elements if not _hollow_wrapper(e, elements)]
    for index, element in enumerate(elements):
        element.index = index
    return elements


def _clean(text: str) -> str:
    return " ".join(text.split())


def _hollow_wrapper(element: Element, elements: list[Element]) -> bool:
    """A textless element (often a Cell) whose labeled content is already listed."""
    if element.label or element.value or element.identifier:
        return False
    return any(
        other is not element and other.type in INTERACTIVE and other.label
        and element.x <= other.center[0] <= element.x + element.width
        and element.y <= other.center[1] <= element.y + element.height
        for other in elements
    )


def render(elements: list[Element]) -> str:
    return "\n".join(element.describe() for element in elements) or "(no visible elements)"
