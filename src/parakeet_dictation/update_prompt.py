"""The Software Update window, which offers a newer version of Maramax with its release notes."""

from __future__ import annotations

import enum
import functools
import re
from collections.abc import Callable
from dataclasses import dataclass

import objc
from AppKit import (
    NSApplication, NSAttributedString, NSBackingStoreBuffered, NSBezelBorder, NSButton, NSColor, NSFont,
    NSFontAttributeName, NSFontItalicTrait, NSFontManager, NSFontWeightSemibold, NSForegroundColorAttributeName,
    NSImageView, NSLayoutConstraint, NSLayoutPriorityDefaultLow, NSLinkAttributeName, NSMakeRect, NSMakeSize,
    NSMutableAttributedString, NSMutableParagraphStyle, NSPanel, NSParagraphStyleAttributeName, NSScrollView,
    NSTextAlignmentLeft, NSTextAlignmentRight, NSTextField, NSTextTab, NSTextView, NSViewWidthSizable,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSObject

from .layout import spacer, stack

MARGIN = 20
ICON = 64
WIDTH = 620
HEIGHT = 440
MIN_WIDTH = 480
MIN_HEIGHT = 340
BULLET_INDENT = 16
# A numbered item's number ends at NUMBER_END, so the dots of 9. and 10. line
# up, and its text starts at NUMBER_INDENT ("99." is 18.5 pt wide at 12 pt).
NUMBER_END = 20
NUMBER_INDENT = 26
HEADLINE = "A new version of Maramax is available!"
NO_NOTES = "This version was published without release notes."


class Choice(enum.Enum):
    INSTALL = "install"
    LATER = "later"     # Remind Me Later: the next check offers it again.
    SKIP = "skip"       # Skip This Version: automatic checks stay quiet about it.


class Emphasis(enum.Enum):
    PLAIN = "plain"
    STRONG = "strong"
    ITALIC = "italic"
    CODE = "code"


@dataclass(frozen=True)
class Run:
    text: str
    emphasis: Emphasis


@dataclass(frozen=True)
class Link:
    text: str
    url: str


Parts = tuple[Run | Link, ...]


@dataclass(frozen=True)
class Heading:
    parts: Parts


@dataclass(frozen=True)
class Bullet:
    parts: Parts


@dataclass(frozen=True)
class NumberedItem:
    number: str     # as written: "2." or "2)"
    parts: Parts


@dataclass(frozen=True)
class Paragraph:
    parts: Parts


Block = Heading | Bullet | NumberedItem | Paragraph


_INLINE = re.compile(
    # Only web links: the notes are not covered by the release's signature.
    r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)"
    r"|\*\*(?P<strong>.+?)\*\*|__(?P<strong2>.+?)__"
    r"|`(?P<code>[^`]+)`"
    r"|(?<!\w)\*(?P<italic>[^*\s][^*]*?)\*|(?<!\w)_(?P<italic2>[^_\s][^_]*?)_(?!\w)"
)
_HEADING = re.compile(r"#{1,6}\s+(.*?)\s*#*\s*")
_BULLET = re.compile(r"\s*[-*+]\s+(.*)")
_NUMBERED = re.compile(r"\s*(\d{1,9}[.)])\s+(.*)")


def inline_parts(text: str) -> Parts:
    """One line of Markdown as runs of text and links, without the markup."""
    parts: list[Run | Link] = []
    position = 0
    for match in _INLINE.finditer(text):
        if match.start() > position:
            parts.append(Run(text[position:match.start()], Emphasis.PLAIN))
        if match["label"] is not None:
            parts.append(Link(match["label"], match["url"]))
        elif match["strong"] is not None or match["strong2"] is not None:
            parts.append(Run(match["strong"] or match["strong2"], Emphasis.STRONG))
        elif match["code"] is not None:
            parts.append(Run(match["code"], Emphasis.CODE))
        else:
            parts.append(Run(match["italic"] or match["italic2"], Emphasis.ITALIC))
        position = match.end()
    if position < len(text):
        parts.append(Run(text[position:], Emphasis.PLAIN))
    return tuple(parts)


def note_blocks(markdown: str) -> list[Block]:
    """Release notes (GitHub Markdown) as headings, bullets, numbered items,
    and paragraphs. A line that continues an item or paragraph joins it, as
    Markdown does."""
    # Each block's text is gathered first (a later line may continue it),
    # with what makes the block once its text is complete.
    blocks: list[tuple[Callable[[Parts], Block], str]] = []
    open_block = False  # Whether the next plain line continues the last block.
    for line in markdown.replace("\r\n", "\n").split("\n"):
        heading, bullet, numbered = _HEADING.fullmatch(line), _BULLET.fullmatch(line), _NUMBERED.fullmatch(line)
        if not line.strip():
            open_block = False
        elif heading:
            blocks.append((Heading, heading.group(1)))
            open_block = False
        elif bullet:
            blocks.append((Bullet, bullet.group(1).strip()))
            open_block = True
        elif numbered:
            blocks.append((functools.partial(NumberedItem, numbered.group(1)), numbered.group(2).strip()))
            open_block = True
        elif open_block:
            make, text = blocks[-1]
            blocks[-1] = (make, f"{text} {line.strip()}")
        else:
            blocks.append((Paragraph, line.strip()))
            open_block = True
    return [make(inline_parts(text)) for make, text in blocks]


def offer_text(version: str, current_version: str) -> str:
    return f"Maramax {version} is now available—you have {current_version}. Would you like to install it now?"


def install_note(size: str) -> str:
    """`size` is the download as people read it ("4.0 MB")."""
    return (f"Installing downloads about {size} and restarts Maramax when it is idle. "
            "Your settings, history, and recordings stay as they are.")


class UpdatePromptWindow(NSObject):
    """Skip This Version, Remind Me Later, Install Update. Closing the window
    is Remind Me Later. `on_choice` is called once per show(), unless the
    offer is withdrawn first."""

    def initWithChoice_(self, on_choice: Callable[[Choice], None]):
        self = objc.super(UpdatePromptWindow, self).init()
        if self is None:
            return None
        self.on_choice = on_choice
        self._waiting = False  # Shown, and not answered yet.
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, False)
        self.panel.setTitle_("Software Update")
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setContentMinSize_(NSMakeSize(MIN_WIDTH, MIN_HEIGHT))
        # Above other windows (the transcript window floats too), so an offer
        # left unanswered stays in sight instead of hiding behind them.
        self.panel.setFloatingPanel_(True)
        self.panel.setDelegate_(self)
        root = self.panel.contentView()

        icon = NSImageView.imageViewWithImage_(NSApplication.sharedApplication().applicationIconImage())
        icon.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.title = NSTextField.labelWithString_(HEADLINE)
        self.title.setFont_(NSFont.boldSystemFontOfSize_(14))
        self.offer = _wrapping("", NSFont.systemFontOfSize_(12))
        notes_title = NSTextField.labelWithString_("Release Notes:")
        notes_title.setFont_(NSFont.boldSystemFontOfSize_(11))
        self.notes = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 200))
        self.notes.setEditable_(False)
        self.notes.setSelectable_(True)
        self.notes.setTextContainerInset_((8, 8))
        self.notes.textContainer().setWidthTracksTextView_(True)
        self.notes.setAutoresizingMask_(NSViewWidthSizable)  # Width follows the scroll view.
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 200))
        scroll.setDocumentView_(self.notes)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setContentHuggingPriority_forOrientation_(NSLayoutPriorityDefaultLow - 1, 1)
        scroll.heightAnchor().constraintGreaterThanOrEqualToConstant_(120).setActive_(True)
        self.note = _wrapping("", NSFont.systemFontOfSize_(11))
        self.note.setTextColor_(NSColor.secondaryLabelColor())
        column = stack([self.title, self.offer, notes_title, scroll, self.note], spacing=8)
        column.setCustomSpacing_afterView_(14, self.offer)
        column.setCustomSpacing_afterView_(4, notes_title)
        for view in (self.title, self.offer, scroll, self.note):
            view.widthAnchor().constraintEqualToAnchor_(column.widthAnchor()).setActive_(True)

        self.skip = NSButton.buttonWithTitle_target_action_("Skip This Version", self, "skipVersion:")
        self.later = NSButton.buttonWithTitle_target_action_("Remind Me Later", self, "remindLater:")
        self.later.setKeyEquivalent_("\x1b")
        self.install = NSButton.buttonWithTitle_target_action_("Install Update", self, "installUpdate:")
        self.install.setKeyEquivalent_("\r")
        buttons = stack([self.skip, spacer(), self.later, self.install], horizontal=True)

        for view in (column, buttons):
            view.setTranslatesAutoresizingMaskIntoConstraints_(False)
            root.addSubview_(view)
        root.addSubview_(icon)
        NSLayoutConstraint.activateConstraints_([
            icon.topAnchor().constraintEqualToAnchor_constant_(root.topAnchor(), MARGIN),
            icon.leadingAnchor().constraintEqualToAnchor_constant_(root.leadingAnchor(), MARGIN),
            icon.widthAnchor().constraintEqualToConstant_(ICON),
            icon.heightAnchor().constraintEqualToConstant_(ICON),
            column.topAnchor().constraintEqualToAnchor_constant_(root.topAnchor(), MARGIN),
            column.leadingAnchor().constraintEqualToAnchor_constant_(icon.trailingAnchor(), 16),
            column.trailingAnchor().constraintEqualToAnchor_constant_(root.trailingAnchor(), -MARGIN),
            buttons.topAnchor().constraintEqualToAnchor_constant_(column.bottomAnchor(), 16),
            buttons.leadingAnchor().constraintEqualToAnchor_(column.leadingAnchor()),
            buttons.trailingAnchor().constraintEqualToAnchor_(column.trailingAnchor()),
            buttons.bottomAnchor().constraintEqualToAnchor_constant_(root.bottomAnchor(), -MARGIN),
        ])
        return self

    @objc.python_method
    def show(self, *, version: str, current_version: str, notes: str, size: str, activate: bool):
        """Offer `version`. Only a check the user asked for (`activate`) takes
        the keyboard: one that ran by itself must not catch what they are
        typing elsewhere, Return least of all."""
        self._waiting = True
        self.offer.setStringValue_(offer_text(version, current_version))
        self.note.setStringValue_(install_note(size))
        self.notes.textStorage().setAttributedString_(rendered_notes(note_blocks(notes)))
        self.notes.scrollRangeToVisible_((0, 0))
        if not self.panel.isVisible():
            self.panel.center()
        if activate:
            self.bring_forward()
        else:
            self.panel.orderFrontRegardless()

    @objc.python_method
    def bring_forward(self):
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def withdraw(self):
        """Take the offer back unanswered: a newer check replaces it."""
        self._waiting = False
        self.panel.orderOut_(None)

    @objc.python_method
    def _answer(self, choice: Choice):
        if not self._waiting:
            return
        self._waiting = False
        self.panel.orderOut_(None)
        self.on_choice(choice)

    def installUpdate_(self, sender):
        del sender
        self._answer(Choice.INSTALL)

    def remindLater_(self, sender):
        del sender
        self._answer(Choice.LATER)

    def skipVersion_(self, sender):
        del sender
        self._answer(Choice.SKIP)

    def windowWillClose_(self, notification):
        del notification
        self._answer(Choice.LATER)


_EMPHASIS_FONTS = {
    Emphasis.PLAIN: lambda size: {},
    Emphasis.STRONG: lambda size: {NSFontAttributeName: NSFont.boldSystemFontOfSize_(size)},
    Emphasis.ITALIC: lambda size: {NSFontAttributeName: NSFontManager.sharedFontManager().convertFont_toHaveTrait_(
        NSFont.systemFontOfSize_(size), NSFontItalicTrait)},
    Emphasis.CODE: lambda size: {NSFontAttributeName: NSFont.monospacedSystemFontOfSize_weight_(size - 1, 0)},
}


def rendered_notes(blocks: list[Block]):
    """The notes as styled text, in colours that follow Light and Dark."""
    text = NSMutableAttributedString.alloc().init()
    if not blocks:
        _append(text, NO_NOTES, {NSFontAttributeName: NSFont.systemFontOfSize_(12),
                                 NSForegroundColorAttributeName: NSColor.secondaryLabelColor()})
        return text
    for index, block in enumerate(blocks):
        style = NSMutableParagraphStyle.alloc().init()
        style.setParagraphSpacing_(5)
        size = 12
        if isinstance(block, Heading):
            size = 13
            style.setParagraphSpacingBefore_(0 if index == 0 else 8)
        elif isinstance(block, Paragraph) and index > 0 and isinstance(blocks[index - 1], Bullet | NumberedItem):
            style.setParagraphSpacingBefore_(6)  # Text after a list stands apart from its last item.
        elif isinstance(block, Bullet):
            style.setHeadIndent_(BULLET_INDENT)
            style.setTabStops_([_tab(NSTextAlignmentLeft, BULLET_INDENT)])
        elif isinstance(block, NumberedItem):
            style.setHeadIndent_(NUMBER_INDENT)
            style.setTabStops_([_tab(NSTextAlignmentRight, NUMBER_END), _tab(NSTextAlignmentLeft, NUMBER_INDENT)])
        base = {NSParagraphStyleAttributeName: style, NSForegroundColorAttributeName: NSColor.labelColor(),
                NSFontAttributeName: (NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold)
                                      if isinstance(block, Heading) else NSFont.systemFontOfSize_(size))}
        if isinstance(block, Bullet):
            _append(text, "•\t", base)
        elif isinstance(block, NumberedItem):
            _append(text, f"\t{block.number}\t", base)
        for part in block.parts:
            url = NSURL.URLWithString_(part.url) if isinstance(part, Link) else None
            if url is not None:
                _append(text, part.text, base | {NSLinkAttributeName: url})
            elif isinstance(part, Link):
                _append(text, part.text, base)  # A URL macOS cannot parse would make a dead link.
            else:
                _append(text, part.text, base | _EMPHASIS_FONTS[part.emphasis](size))
        if index < len(blocks) - 1:
            _append(text, "\n", base)
    return text


def _tab(alignment, location):
    return NSTextTab.alloc().initWithTextAlignment_location_options_(alignment, location, {})


def _append(text, string, attributes):
    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(string, attributes))


def _wrapping(text, font):
    label = NSTextField.wrappingLabelWithString_(text)
    label.setFont_(font)
    label.setSelectable_(False)
    return label

