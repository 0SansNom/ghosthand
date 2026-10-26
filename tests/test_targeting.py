from ghosthand.targeting import element_at, moved, point_changed, relocate
from ghosthand.tree import Element


def el(index, label, y, type_="Button", identifier="", height=40):
    return Element(index, type_, 0, y, 400, height, label=label, identifier=identifier)


def test_relocate_finds_the_same_element_even_after_it_moved():
    target = el(3, "General", y=400)
    fresh = [el(0, "Back", y=50), el(1, "General", y=250)]
    found = relocate(target, fresh)
    assert found is fresh[1] and moved(target, found)


def test_relocate_stays_put_and_prefers_the_closest_twin():
    target = el(2, "Edit", y=300)
    fresh = [el(0, "Edit", y=100), el(1, "Edit", y=302)]
    found = relocate(target, fresh)
    assert found is fresh[1] and not moved(target, found)


def test_relocate_reports_a_vanished_element():
    assert relocate(el(0, "Done", y=100), [el(0, "Cancel", y=100)]) is None


def test_unlabeled_elements_only_match_nearby():
    target = el(0, "", y=100, type_="Cell")
    assert relocate(target, [el(0, "", y=110, type_="Cell")]) is not None
    assert relocate(target, [el(0, "", y=600, type_="Cell")]) is None


def test_point_changed():
    general = el(0, "General", y=100)
    assert not point_changed(general, el(5, "General", y=100))
    assert point_changed(general, el(5, "Delete", y=100))
    assert point_changed(general, None)
    assert not point_changed(None, None)
    assert not point_changed(el(0, "", y=100, type_="Other"), None)


def test_element_at_picks_the_smallest_element_under_the_point():
    cell = Element(0, "Cell", 0, 100, 400, 60, label="Wi-Fi")
    switch = Element(1, "Switch", 330, 110, 50, 30, label="Wi-Fi")
    assert element_at([cell, switch], 350, 120) is switch
    assert element_at([cell, switch], 100, 120) is cell
    assert element_at([cell, switch], 100, 500) is None
