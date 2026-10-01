"""Checkbox list for the terminal (stdlib ``curses``): arrows move, space toggles, enter confirms.

``PickerState`` holds the key handling so it can be tested without a terminal;
``pick()`` is the thin curses loop around it.
"""

from __future__ import annotations

import curses
import os

_ARROWS = {"[A": "KEY_UP", "OA": "KEY_UP", "[B": "KEY_DOWN", "OB": "KEY_DOWN"}

_UP = {"KEY_UP", "k"}
_DOWN = {"KEY_DOWN", "j"}
_CONFIRM = {"\n", "\r", "KEY_ENTER"}
_CANCEL = {"q", "\x1b"}  # q or Esc


class PickerState:
    def __init__(self, items: list[str], preselected: set[str] | None = None) -> None:
        self.items = list(items)
        self.selected: set[str] = set(preselected or ())
        self.cursor = 0

    def handle(self, key: str) -> str | None:
        """Apply one key. Returns ``"confirm"``, ``"cancel"`` or ``None`` to keep going."""
        if key in _UP:
            self.cursor = max(0, self.cursor - 1)
        elif key in _DOWN:
            self.cursor = min(len(self.items) - 1, self.cursor + 1)
        elif key == " ":
            self.selected ^= {self.items[self.cursor]}
        elif key == "a":
            self.selected = set() if len(self.selected) == len(self.items) else set(self.items)
        elif key in _CONFIRM:
            return "confirm"
        elif key in _CANCEL:
            return "cancel"
        return None

    def result(self) -> list[str]:
        return [i for i in self.items if i in self.selected]


def _read_key(screen: curses.window) -> str:
    """``getkey`` plus a fallback for arrow sequences terminfo did not recognise (``ESC [ B``)."""
    key = screen.getkey()
    if key != "\x1b":
        return key
    screen.nodelay(True)
    try:
        rest = ""
        for _ in range(2):
            try:
                rest += screen.getkey()
            except curses.error:
                break
    finally:
        screen.nodelay(False)
    return _ARROWS.get(rest, "\x1b")


def _loop(screen: curses.window, state: PickerState, title: str) -> list[str] | None:
    curses.curs_set(0)
    screen.keypad(True)
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        try:
            if height < 5 or width < 24:
                screen.addnstr(0, 0, "Terminal too small", max(1, width - 1))
            else:
                screen.addnstr(0, 0, title, width - 1, curses.A_BOLD)
                screen.addnstr(1, 0, "↑/↓ move · space toggle · a all · enter install · q cancel", width - 1, curses.A_DIM)
                rows = height - 3
                top = min(max(0, state.cursor - rows + 1), max(0, len(state.items) - rows))
                for row, item in enumerate(state.items[top : top + rows]):
                    mark = "x" if item in state.selected else " "
                    attr = curses.A_REVERSE if top + row == state.cursor else curses.A_NORMAL
                    screen.addnstr(row + 2, 0, f"[{mark}] {item}", width - 1, attr)
        except curses.error:
            pass  # a mid-resize frame that does not fit; the next KEY_RESIZE redraws
        screen.refresh()
        action = state.handle(_read_key(screen))
        if action == "confirm":
            return state.result()
        if action == "cancel":
            return None


def pick(items: list[str], title: str = "Select") -> list[str] | None:
    """Full-screen checkbox list. Returns the chosen items in list order, or ``None`` on cancel."""
    os.environ.setdefault("ESCDELAY", "25")  # don't make Esc wait a full second
    state = PickerState(items)
    return curses.wrapper(_loop, state, title)
