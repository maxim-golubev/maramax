"""The pieces Maramax's native windows are laid out with: stacks, a spacer, and the small grey explanatory text."""

from __future__ import annotations

from AppKit import (
    NSColor, NSFont, NSLayoutAttributeFirstBaseline, NSLayoutAttributeLeading, NSLayoutPriorityDefaultLow,
    NSMakeRect, NSStackView, NSTextField, NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical, NSView,
)


def stack(views, *, horizontal=False, spacing=8):
    """A row or a column. Rows line up on the first baseline and columns on
    the leading edge, both of which work on alignment rectangles, so push
    buttons, popups, and text line up on what the eye sees as their edges."""
    view = NSStackView.stackViewWithViews_(views)
    view.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal if horizontal
                         else NSUserInterfaceLayoutOrientationVertical)
    view.setAlignment_(NSLayoutAttributeFirstBaseline if horizontal else NSLayoutAttributeLeading)
    view.setSpacing_(spacing)
    return view


def spacer():
    """A view that takes up the free width in a row."""
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
    view.setContentHuggingPriority_forOrientation_(NSLayoutPriorityDefaultLow - 1, 0)
    return view


def small_text(text, width):
    """Secondary 11 pt text that wraps at `width`: help, notes, and hints."""
    label = NSTextField.wrappingLabelWithString_(text)
    label.setFont_(NSFont.systemFontOfSize_(11))
    label.setTextColor_(NSColor.secondaryLabelColor())
    label.setSelectable_(False)
    label.setPreferredMaxLayoutWidth_(width)
    return label
