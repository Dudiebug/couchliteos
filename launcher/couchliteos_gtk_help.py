"""The tour and the HELP page on screen (GTK 4). What they say, and what each key does, is
couchliteos_help's (tested on its own); here it is only drawn.

The tour (TourPage) is a band across the whole screen, as the PS3's information panels were:
thin rules above and below, the wave showing through it, the card inside. A card slides in
from the side it comes from (MOTION FULL), cross-fades (REDUCED) or just changes (OFF).

HELP (HelpPage) is laid out as SETTINGS: the topics on the left, the focused one's text on the
right. Going into the text (RIGHT or A) lets UP / DOWN scroll it; the scroll eases with MOTION.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

import couchliteos_help as tvhelp  # noqa: E402
import couchliteos_motion as motion  # noqa: E402

ICON_PX = 200  # a card's icon at 1080p
BAND_PX = 520  # the band's least height at 1080p: the cards do not make it jump


def _label(text: str, *classes: str, xalign: float = 0.0, wrap: bool = False) -> Gtk.Label:
    """As couchliteos-tv's label(): a wrapped label, or one line at its own width."""
    widget = Gtk.Label(label=text, xalign=xalign)
    for name in classes:
        widget.add_css_class(name)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return widget


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def _rule() -> Gtk.Box:
    rule = Gtk.Box()
    rule.add_css_class("tv-rule")
    return rule


def transition(direction: int, level: str) -> tuple[Gtk.StackTransitionType, int]:
    """How a card replaces the one before it, and in how many ms."""
    ms = round(motion.scaled(motion.PAGE_MS, level) * 1000)
    if direction == 0 or ms <= 0:
        return Gtk.StackTransitionType.NONE, 0
    if level == motion.REDUCED:
        return Gtk.StackTransitionType.CROSSFADE, ms
    return (Gtk.StackTransitionType.SLIDE_LEFT if direction > 0 else Gtk.StackTransitionType.SLIDE_RIGHT), ms


# ---------------------------------------------------------------------- the tour


class CardBox(Gtk.Overlay):
    """One card: the icon on the left; the title, the lines and the buttons beside it. The icon
    lies over the column's left margin: a horizontal Gtk.Box gave its wrapping labels more width
    than the card had (they asked for it at the box's height), past the screen at 720p."""

    def __init__(self) -> None:
        super().__init__()
        self.icon = Gtk.Picture()
        self.icon.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.icon.set_halign(Gtk.Align.START)
        self.icon.set_valign(Gtk.Align.CENTER)
        self.icon.add_css_class("tv-card-icon")
        column = self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.set_hexpand(True)
        column.set_valign(Gtk.Align.CENTER)
        self.title = _label("", "tv-card-title", wrap=True)
        self.lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.chips = Gtk.Grid()
        for widget in (self.title, self.lines, self.chips):
            column.append(widget)
        self.set_child(column)
        self.add_overlay(self.icon)

    def relayout(self, px: Callable[[float], int], margin_x: int) -> None:
        self.column.set_margin_start(px(ICON_PX) + px(56))
        self.column.set_size_request(-1, px(ICON_PX))  # the icon is not measured: the card is at least as tall
        self.set_margin_start(margin_x)
        self.set_margin_end(margin_x)
        self.icon.set_size_request(px(ICON_PX), px(ICON_PX))
        self.column.set_spacing(px(22))
        self.lines.set_spacing(px(10))
        self.chips.set_row_spacing(px(14))
        self.chips.set_column_spacing(px(24))

    def fill(self, view: tvhelp.CardView, texture: Gdk.Texture | None) -> None:
        self.icon.set_paintable(texture)
        self.title.set_label(view.title)
        _clear(self.lines)
        for line in view.lines:
            self.lines.append(_label(line, "tv-card-line", wrap=True))
        _clear(self.chips)
        for row, (keys, words) in enumerate(view.chips):
            pill = _label(keys, "tv-chip-keys", xalign=0.5)
            pill.set_halign(Gtk.Align.START)
            pill.set_valign(Gtk.Align.CENTER)
            what = _label(words, "tv-chip-words", wrap=True)
            what.set_hexpand(True)
            what.set_valign(Gtk.Align.CENTER)
            self.chips.attach(pill, 0, row, 1, 1)
            self.chips.attach(what, 1, row, 1, 1)
        self.chips.set_visible(bool(view.chips))


class TourPage(Gtk.Box):
    """The tour's page in the TV's stack. `icon_path(name)` is a bundled icon's file."""

    def __init__(self, icon_path: Callable[[str], pathlib.Path]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.icon_path = icon_path
        self.textures: dict[tuple[str, int], Gdk.Texture | None] = {}
        self.icon_px = ICON_PX
        above = Gtk.Box()
        above.set_vexpand(True)
        self.append(above)
        self.append(_rule())
        band = self.band = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        band.add_css_class("tv-band")
        self.slides = Gtk.Stack()
        self.slides.set_vexpand(True)
        self.slots = (CardBox(), CardBox())
        # Each card in a viewport that never scrolls: a card's wrapping labels can ask for a minimum
        # height far over the screen's (GTK measures them at odd widths), which grew the window
        # past the screen; the viewport asks for none and the card is laid out at the band's size.
        self.frames = []
        for slot in self.slots:
            frame = Gtk.ScrolledWindow()
            frame.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
            frame.set_propagate_natural_height(True)  # as tall as the card wants, when it fits
            frame.set_child(slot)
            frame.get_child().set_hscroll_policy(Gtk.ScrollablePolicy.MINIMUM)  # the viewport: the card at the band's width
            self.frames.append(frame)
            self.slides.add_child(frame)
        self.front = 0
        band.append(self.slides)
        self.append(band)
        self.append(_rule())
        self.dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.dots.set_halign(Gtk.Align.CENTER)
        self.append(self.dots)
        below = Gtk.Box()
        below.set_vexpand(True)
        self.append(below)
        self.prompt = _label("", "tv-prompt", xalign=0.5, wrap=True)
        self.append(self.prompt)

    def relayout(self, layout) -> None:
        """`layout` is a tvlayout.Layout: the band spans the screen, its card keeps the margins."""
        px = layout.px
        self.set_margin_top(layout.margin_y)
        self.set_margin_bottom(layout.margin_y)
        self.set_spacing(px(18))
        self.slides.set_size_request(-1, px(BAND_PX))
        self.band.set_margin_top(0)
        for slot in self.slots:
            slot.relayout(px, layout.margin_x)
            slot.set_margin_top(px(36))
            slot.set_margin_bottom(px(36))
        self.dots.set_spacing(px(14))
        self.dots.set_margin_top(px(10))
        self.prompt.set_margin_start(layout.margin_x)
        self.prompt.set_margin_end(layout.margin_x)
        self.icon_px = px(ICON_PX)

    def texture(self, name: str) -> Gdk.Texture | None:
        key = (name, self.icon_px)
        if key not in self.textures:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(self.icon_path(name)), self.icon_px, self.icon_px, True)
                self.textures[key] = Gdk.Texture.new_for_pixbuf(pixbuf)
            except GLib.Error:
                self.textures[key] = None  # the card still reads without its picture
        return self.textures[key]

    def show_card(self, view: tvhelp.CardView, direction: int, level: str) -> None:
        """`view` in the slot behind, then brought in: from the right going on (`direction` 1),
        from the left going back (-1), at once (0: the tour opening, a new size)."""
        kind, ms = transition(direction, level)
        back = self.slots[1 - self.front]
        back.fill(view, self.texture(view.icon))
        self.slides.set_transition_type(kind)
        self.slides.set_transition_duration(ms)
        self.slides.set_visible_child(self.frames[1 - self.front])
        self.front = 1 - self.front
        _clear(self.dots)
        for index in range(view.count):
            dot = Gtk.Box()
            dot.add_css_class("tv-dot")
            if index == view.index:
                dot.add_css_class("tv-on")
            dot.set_valign(Gtk.Align.CENTER)
            self.dots.append(dot)
        self.prompt.set_label(view.prompt)


# ---------------------------------------------------------------------- HELP


class HelpPage(Gtk.Box):
    """HELP's page in the TV's stack: the title, the topics, the focused topic's text, the prompt."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.append(_label("HELP", "tv-title"))
        panes = self.panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        panes.set_vexpand(True)
        self.topic_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.topic_list.add_css_class("tv-list")
        self.topic_list.set_hexpand(False)
        panes.append(self.topic_list)
        pane = self.pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.add_css_class("tv-help-pane")
        pane.set_hexpand(True)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)  # the D-pad scrolls it
        self.scroller.set_vexpand(True)
        text = self.text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.title = _label("", "tv-help-title", wrap=True)
        self.lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.append(self.title)
        text.append(self.lines)
        self.scroller.set_child(text)
        pane.append(self.scroller)
        panes.append(pane)
        self.append(panes)
        self.prompt = _label("", "tv-prompt", xalign=0.5, wrap=True)
        self.append(self.prompt)
        self.slots = 8
        self.tween: motion.Tween | None = None
        self.tick_id = 0

    def relayout(self, layout, slots: int) -> None:
        """`layout` is a tvlayout.Layout; `slots` how many topics fit (tvscreens.list_slots)."""
        px = layout.px
        for side in ("start", "end"):
            getattr(self, f"set_margin_{side}")(layout.margin_x)
        self.set_margin_top(layout.margin_y)
        self.set_margin_bottom(layout.margin_y)
        self.set_spacing(px(16))
        self.panes.set_spacing(px(40))
        self.topic_list.set_size_request(round((layout.width - 2 * layout.margin_x) * 0.36), -1)
        self.text.set_spacing(px(20))
        self.lines.set_spacing(px(18))
        self.slots = slots

    def render(self, model: tvhelp.HelpModel, family: str) -> None:
        _clear(self.topic_list)
        for index in model.visible(self.slots):
            item = _label(model.topics[index].title, "tv-item")
            item.set_ellipsize(Pango.EllipsizeMode.END)
            item.set_max_width_chars(1)  # the list's width, not the title's (as SETTINGS' rows)
            item.set_hexpand(True)
            if index == model.focus:
                item.add_css_class("tv-open" if model.reading else "tv-focused")
            self.topic_list.append(item)
        topic = model.topic
        if self.title.get_label() != topic.title:
            self.title.set_label(topic.title)
            _clear(self.lines)
            for line in topic.lines:
                self.lines.append(_label(line, "tv-help-line", wrap=True))
            self.scroll_to_top()
        (self.pane.add_css_class if model.reading else self.pane.remove_css_class)("tv-reading")
        self.prompt.set_label(model.prompt(family))

    # -------------------------------------------------------------- scrolling

    def scroll_to_top(self) -> None:
        self.tween = None
        self.scroller.get_vadjustment().set_value(0)

    def scroll(self, direction: int, level: str) -> None:
        """Half a pane up (-1) or down (1), eased; a press while it moves goes on from there."""
        adjustment = self.scroller.get_vadjustment()
        bottom = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
        start = adjustment.get_value()
        aim = self.tween.end if self.tween is not None else start
        target = max(0.0, min(bottom, aim + direction * adjustment.get_page_size() * 0.5))
        seconds = motion.scaled(motion.PAGE_MS, level)
        if seconds <= 0 or target == start:
            self.tween = None
            adjustment.set_value(target)
            return
        self.tween = motion.Tween(start, target, time.monotonic(), seconds)
        if not self.tick_id:
            self.tick_id = self.add_tick_callback(self.on_tick)

    def on_tick(self, _widget, _clock) -> bool:
        if self.tween is None:
            self.tick_id = 0
            return False
        now = time.monotonic()
        self.scroller.get_vadjustment().set_value(self.tween.value(now))
        if self.tween.done(now):
            self.tween = None
            self.tick_id = 0
            return False
        return True
