"""The pieces Maramax's native windows are laid out with: stacks, a spacer, widths, and small grey text."""

from __future__ import annotations

import enum

from AppKit import (
    NSAttributedString, NSColor, NSFont, NSFontAttributeName, NSFontWeightRegular, NSForegroundColorAttributeName,
    NSImage, NSImageSymbolConfiguration, NSLayoutAttributeFirstBaseline, NSLayoutAttributeLeading,
    NSLayoutPriorityDefaultLow, NSMakeRect, NSStackView, NSTextAttachment, NSTextField,
    NSUserInterfaceLayoutOrientationHorizontal, NSUserInterfaceLayoutOrientationVertical, NSView,
)

SMALL_TEXT_SIZE = 11


class Notice(enum.Enum):
    """What a notice says about a state, as an SF Symbol and the colour's NSColor selector."""
    ALLOWED = ("checkmark.circle.fill", "systemGreenColor")
    WARNING = ("exclamationmark.triangle.fill", "systemOrangeColor")


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


def aligned_width(control):
    """A control's natural width as auto layout and the eye measure it:
    without the margin a push button draws no bezel in."""
    size = control.fittingSize()
    return control.alignmentRectForFrame_(NSMakeRect(0, 0, size.width, size.height)).size.width


def small_text(text, width):
    """Secondary 11 pt text that wraps at `width`: help, notes, and hints."""
    label = NSTextField.wrappingLabelWithString_(text)
    label.setFont_(NSFont.systemFontOfSize_(SMALL_TEXT_SIZE))
    label.setTextColor_(NSColor.secondaryLabelColor())
    label.setSelectable_(False)
    label.setPreferredMaxLayoutWidth_(width)
    return label


def show_notice(label, notice: Notice, text):
    """Give a small_text() label the notice's symbol before `text`: a state
    the eye should catch, such as a warning. The symbol is part of the text,
    so it sits on the text's baseline and wraps with it."""
    symbol, tint = notice.value
    size = NSImageSymbolConfiguration.configurationWithPointSize_weight_(SMALL_TEXT_SIZE, NSFontWeightRegular)
    color = NSImageSymbolConfiguration.configurationWithPaletteColors_([getattr(NSColor, tint)()])
    attachment = NSTextAttachment.alloc().init()
    attachment.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
                         .imageWithSymbolConfiguration_(size.configurationByApplyingConfiguration_(color)))
    rich = NSAttributedString.attributedStringWithAttachment_(attachment).mutableCopy()
    rich.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
        f" {text}", {NSFontAttributeName: label.font(), NSForegroundColorAttributeName: label.textColor()}))
    label.setAttributedStringValue_(rich)
