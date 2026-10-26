from ghosthand.tree import flatten, render


def frame(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


SETTINGS = {
    "type": "Application",
    "frame": frame(0, 0, 402, 874),
    "label": "Réglages",
    "children": [
        {"type": "StaticText", "label": "Réglages", "frame": frame(20, 120, 140, 40)},
        {
            # Textless cell wrapping a labeled button: only the button should remain.
            "type": "Cell",
            "frame": frame(16, 430, 370, 52),
            "children": [
                {
                    "type": "Button",
                    "label": "Général",
                    "identifier": "com.apple.settings.general",
                    "frame": frame(16, 430, 370, 52),
                    "children": [{"type": "StaticText", "label": "Général", "frame": frame(60, 440, 80, 30)}],
                }
            ],
        },
        {"type": "TextField", "placeholder": "Rechercher", "frame": frame(16, 200, 370, 36), "focused": True},
        {"type": "Button", "label": "Off screen", "frame": frame(16, 2000, 370, 52)},
        {"type": "Button", "label": "Hidden", "frame": frame(16, 300, 0, 0)},
        {"type": "Toolbar", "label": "Barre d’outils", "frame": frame(0, 0, 402, 874)},
        {"type": "Other", "label": "Compte\u00a0Apple", "frame": frame(0, 500, 100, 20)},
        # Exact duplicate (e.g. twin scroll bars) is listed once.
        {"type": "Other", "label": "Compte\u00a0Apple", "frame": frame(0, 500, 100, 20)},
        {"type": "Cell", "label": "safari", "frame": frame(0, 600, 402, 50),
         "children": [{"type": "Other", "label": "safari", "frame": frame(0, 600, 402, 50)}]},
    ],
}


def test_flatten_keeps_meaningful_visible_elements_in_order():
    elements = flatten(SETTINGS, 402, 874)
    assert [(e.index, e.type, e.label) for e in elements] == [
        (0, "StaticText", "Réglages"),
        (1, "Button", "Général"),
        (2, "TextField", ""),
        (3, "Other", "Compte Apple"),
        (4, "Cell", "safari"),
    ]


def test_center_and_render():
    general = flatten(SETTINGS, 402, 874)[1]
    assert general.center == (201, 456)
    text = render(flatten(SETTINGS, 402, 874))
    assert '[1] Button "Général" id=com.apple.settings.general @(201,456)' in text
    assert '[2] TextField placeholder="Rechercher" (focused) @(201,218)' in text


def test_render_empty_screen():
    assert render([]) == "(no visible elements)"


def test_focused_flag_only_on_text_inputs():
    web = {"type": "Link", "label": "Apple", "frame": frame(0, 0, 50, 20), "focused": True}
    (link,) = flatten(web, 402, 874)
    assert "focused" not in link.describe()
