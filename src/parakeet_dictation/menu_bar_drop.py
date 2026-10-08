"""Dropping media files on Maramax's menu bar icon."""

from __future__ import annotations

import objc
from AppKit import NSDragOperationCopy, NSDragOperationNone
from Foundation import NSObject

from . import media_drop


class MenuBarDrop(NSObject):
    """Makes the menu bar icon (`button`, the status item's) take dragged
    media: while such a drag is over it the icon is highlighted and the
    pointer shows a copy, and a drop hands its pasteboard to `receive`,
    which says whether it took anything. `accepts()` says whether files can
    be taken right now. Keep a reference to this object: the window it
    listens through does not."""

    def initWithButton_accepts_receive_(self, button, accepts, receive):
        self = objc.super(MenuBarDrop, self).init()
        if self is None:
            return None
        self._button = button
        self._accepts = accepts
        self._receive = receive
        # A window offers a drag to its delegate when no view of its own
        # takes it. The status item's window has none; one that did would
        # stop hearing from its window if it were replaced.
        window = button.window()
        if window.delegate() is not None:
            raise RuntimeError(f"The menu bar icon's window already has a delegate: {window.delegate()!r}")
        window.registerForDraggedTypes_(media_drop.drag_types())
        window.setDelegate_(self)
        return self

    def draggingEntered_(self, sender):
        if not self._accepts() or not media_drop.offered_count(sender.draggingPasteboard()):
            return NSDragOperationNone
        self._button.highlight_(True)
        return NSDragOperationCopy

    def draggingExited_(self, sender):
        del sender
        self._button.highlight_(False)

    def draggingEnded_(self, sender):
        del sender
        self._button.highlight_(False)

    def prepareForDragOperation_(self, sender):
        del sender
        return True

    def performDragOperation_(self, sender):
        self._button.highlight_(False)
        return bool(self._accepts() and self._receive(sender.draggingPasteboard()))
