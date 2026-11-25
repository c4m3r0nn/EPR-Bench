"""Keys on every browser route (claude_browser.Screen, shared by the CLI route and both SDK harnesses), in real Chromium
on this platform: each key combination an agent sends must leave the text of an input as it would on Linux, where the
agent is told nothing of the platform. Combinations that are browser shortcuts (they would leave the page or open
browser tools) are refused; held modifiers on clicks, drags and scrolls are never translated."""

import pytest

from eprbench.agents import claude_browser
from eprbench.agents.claude_browser import Screen, key_combo

sync_api = pytest.importorskip("playwright.sync_api")

PAGE = """<input id="i" style="width:600px"><br><textarea id="t" rows="5" cols="60"></textarea>
<div id="target" style="width:200px;height:60px;background:#ccc">target</div><div id="log"></div>
<script>for (const k of ["mousedown", "wheel"]) document.getElementById("target").addEventListener(k, e => {
  document.getElementById("log").textContent += `${e.type}:${e.ctrlKey},${e.metaKey};`; if (k === "wheel") e.preventDefault(); }, {passive: false});</script>"""
LINES = ["one two", "three four", "five six"]


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport=claude_browser.VIEWPORT)
        yield pg
        browser.close()


def centre(page, selector):
    box = page.locator(selector).bounding_box()
    return [round(box["x"] + 10), round(box["y"] + box["height"] / 2)]


def fill(page, screen, field, text, before=()):
    """Type into an empty field as an agent would (click, type), then press `before`, every key through the executor."""
    page.set_content(PAGE)
    screen.run("left_click", {"coordinate": centre(page, "#" + field)})
    if field == "t":
        for n, line in enumerate(text):
            if n:
                screen.run("key", {"text": "Return"})
            screen.run("type", {"text": line})
    else:
        screen.run("type", {"text": text})
    for k in before:
        screen.run("key", {"text": k})


# (field, text typed, keys pressed first, the combination under test, the field's text after it and then "X" typed),
# the last as Linux Chromium leaves it.
CASES = [
    ("i", "hello big world", [], "ctrl+a", "X"),  # select all
    ("i", "abc", ["ctrl+a", "ctrl+c", "Right"], "ctrl+v", "abcabcX"),  # copy, paste
    ("i", "abc", ["ctrl+a", "ctrl+x"], "ctrl+v", "abcX"),  # cut, paste
    ("i", "abc", [], "ctrl+z", "X"),  # undo
    ("i", "abc", ["ctrl+z"], "ctrl+y", "abcX"),  # redo
    ("i", "abc", ["ctrl+z"], "ctrl+shift+z", "abcX"),  # redo
    ("i", "hello big world", [], "ctrl+Left", "hello big Xworld"),  # a word left
    ("i", "hello big world", ["Home"], "ctrl+Right", "helloX big world"),  # a word right (to its end, as on Linux)
    ("i", "hello big world", [], "ctrl+shift+Left", "hello big X"),  # select a word left
    ("i", "hello big world", ["Home"], "ctrl+shift+Right", "X big world"),  # select a word right
    ("i", "hello big world", [], "ctrl+BackSpace", "hello big X"),  # delete a word back
    ("i", "hello big world", ["Home"], "ctrl+Delete", "X big world"),  # delete a word forward
    ("i", "hello big world", ["Left", "Left"], "Home", "Xhello big world"),
    ("i", "hello big world", ["Home"], "End", "hello big worldX"),
    ("i", "hello big world", ["Left", "Left"], "ctrl+b", "hello big worXld"),  # an unbound Ctrl+letter changes nothing
    ("i", "hello big world", ["Left", "Left"], "ctrl+f", "hello big worXld"),
    ("t", LINES, ["Up", "Left"], "Home", "one two\nXthree four\nfive six"),  # line start
    ("t", LINES, ["Up", "Left"], "End", "one two\nthree fourX\nfive six"),  # line end
    ("t", LINES, ["Up", "Left"], "shift+Home", "one two\nXour\nfive six"),  # select to line start
    ("t", LINES, ["Up", "Left"], "shift+End", "one two\nthree fX\nfive six"),  # select to line end
    ("t", LINES, ["Up", "Left"], "ctrl+Home", "Xone two\nthree four\nfive six"),  # document start
    ("t", LINES, ["Up", "Left"], "ctrl+End", "one two\nthree four\nfive sixX"),  # document end
    ("t", LINES, ["Up", "Left"], "ctrl+shift+Home", "Xour\nfive six"),  # select to document start
    # in a textarea Chromium on Linux moves a line for Ctrl+Up/Down, as for Up/Down (observed in Chromium on Linux)
    ("t", LINES, ["Up", "Left"], "ctrl+Up", "one twoX\nthree four\nfive six"),
    ("t", LINES, ["Up", "Left"], "ctrl+Down", "one two\nthree four\nfive siXx"),
]  # fmt: skip


@pytest.mark.parametrize("field, text, before, combo, expected", CASES, ids=[f"{c[0]}:{'+'.join(c[2])}>{c[3]}" for c in CASES])
def test_a_key_combination_edits_text_as_on_linux(page, field, text, before, combo, expected):
    screen = Screen(page)
    fill(page, screen, field, text, before)
    screen.run("key", {"text": combo})
    screen.run("type", {"text": "X"})
    assert page.input_value("#" + field) == expected


@pytest.mark.parametrize("combo", [
    "ctrl+l", "ctrl+t", "ctrl+n", "ctrl+w", "ctrl+q", "ctrl+r", "F5", "ctrl+shift+r", "ctrl+o", "ctrl+h", "ctrl+j", "ctrl+u", "ctrl+p",
    "ctrl+s", "ctrl+d", "ctrl+k", "ctrl+e", "ctrl+shift+n", "ctrl+shift+t", "ctrl+shift+w", "ctrl+shift+i", "ctrl+shift+j", "ctrl+shift+c",
    "ctrl+shift+Delete", "F12", "F6", "F11", "F1", "ctrl+Tab", "ctrl+shift+Tab", "ctrl+Page_Down", "ctrl+Prior", "ctrl+1", "ctrl+9",
    "alt+Left", "alt+Right", "alt+Home", "alt+F4", "alt+d", "cmd+w", "super+l", "cmd+alt+i", "cmd+bracketleft", "cmd+[",
])  # fmt: skip
def test_browser_shortcuts_are_refused_and_change_nothing(page, combo):
    screen = Screen(page)
    fill(page, screen, "i", "hello")
    url = page.url
    with pytest.raises(ValueError, match="browser shortcut"):
        screen.run("key", {"text": combo})
    assert page.input_value("#i") == "hello" and page.url == url


@pytest.mark.parametrize("action, args", [
    ("left_click", {}),
    ("left_click_drag", {"start_coordinate": None}),
    ("scroll", {"scroll_direction": "down", "scroll_amount": 1}),
])  # fmt: skip
def test_a_modifier_held_on_a_click_drag_or_scroll_stays_control(page, action, args):
    page.set_content(PAGE)
    at = centre(page, "#target")
    args = {**args, "coordinate": at, "text": "ctrl"} | ({"start_coordinate": at} if "start_coordinate" in args else {})
    Screen(page).run(action, args)
    log = page.text_content("#log")
    assert log and all(entry.endswith(":true,false") for entry in log.strip(";").split(";")), log


def test_the_translation_is_stated_exactly():
    """What is pressed for each combination (macOS translates; elsewhere a combination is pressed as given)."""
    mac = {"ctrl+a": "Meta+a", "ctrl+shift+z": "Meta+Shift+z", "ctrl+y": "Meta+Shift+z", "ctrl+Left": "Alt+ArrowLeft", "ctrl+BackSpace": "Alt+Backspace",
           "ctrl+Home": "Meta+ArrowUp", "ctrl+shift+End": "Meta+Shift+ArrowDown", "Home": "Meta+ArrowLeft", "shift+End": "Meta+Shift+ArrowRight",
           "ctrl+Up": "ArrowUp", "ctrl+shift+Down": "Shift+ArrowDown", "Return": "Enter", "shift+Tab": "Shift+Tab", "cmd+a": "Meta+a", "alt+b": "Alt+b"}  # fmt: skip
    for combo, pressed in mac.items():
        assert key_combo(combo, "darwin") == pressed, combo
    linux = {"ctrl+a": "Control+a", "ctrl+Left": "Control+ArrowLeft", "Home": "Home", "ctrl+y": "Control+y", "shift+Page_Down": "Shift+PageDown"}
    for combo, pressed in linux.items():
        assert key_combo(combo, "linux") == pressed, combo
