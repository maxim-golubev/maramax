"""The word-replacement list in Settings: every rule on view, each one editable where it is shown."""

from __future__ import annotations

import math

import objc
from AppKit import (
    NSBezelBorder, NSButton, NSColor, NSLayoutConstraint, NSLineBreakByTruncatingTail, NSMakeRect, NSScrollView,
    NSTableCellView, NSTableColumn, NSTableHeaderCell, NSTableView, NSTableViewLastColumnOnlyAutoresizingStyle,
    NSTableViewStyleFullWidth, NSTextField, NSTextFieldRoundedBezel, NSTextView,
)
from Foundation import NSIndexSet, NSObject

from .corrections import MAX_RULES, RuleRefused, apply_replacements, edited_rules, find_rule, rule_key
from .layout import aligned_width, small_text, spacer, stack

HEARD = "heard"
REPLACEMENT = "replacement"
_TITLES = {HEARD: "When the transcript says", REPLACEMENT: "Replace with"}
VISIBLE_ROWS = 7
GAP = 8
GUIDE = "Double-click a replacement to change it. Replaced text is never replaced a second time."
EMPTY_GUIDE = "No replacements yet. Type what the transcript says and how it should be written, then Add."
# Shown in place of a line break in a replacement of several lines.
_LINE_BREAK = " ⏎ "
_DELETE_KEYS = ("\x7f", "\uf728")  # Delete, Forward Delete
# How far right of its column a cell's text starts beyond its leading
# constraint: the list's border, the cell's place in its column, and the
# label's own padding (measured; tests/test_preferences.py holds it to the
# fields above the list).
_CELL_TEXT_OFFSET = 5
# How far left of its column's text a header cell draws its title (measured likewise).
_HEADER_TEXT_SHIFT = 2


def count_text(count: int) -> str:
    if count == MAX_RULES:
        return f"{count} replacements, the most there can be"
    return "1 replacement" if count == 1 else f"{count} replacements"


def displayed_rules(rules: list[dict[str, str]]) -> list[dict[str, str]]:
    """The order the list shows: alphabetical by what is heard, so a rule is easy to find."""
    return sorted(rules, key=lambda rule: rule_key(rule["heard"]))


def editable_in_place(text: str) -> bool:
    """A table cell edits one line; a replacement of several lines is changed by removing it and adding it again."""
    return "\n" not in text


class RuleHeaderCell(NSTableHeaderCell):
    """A column title that starts where the column's text does."""

    def drawingRectForBounds_(self, bounds):
        rect = objc.super(RuleHeaderCell, self).drawingRectForBounds_(bounds)
        return NSMakeRect(rect.origin.x + _HEADER_TEXT_SHIFT, rect.origin.y,
                          rect.size.width - _HEADER_TEXT_SHIFT, rect.size.height)


class RuleTable(NSTableView):
    """Delete and Forward Delete remove the selected rules."""

    def keyDown_(self, event):
        if event.charactersIgnoringModifiers() in _DELETE_KEYS and self.selectedRowIndexes().count():
            self.delegate().removeRules_(self)
        else:
            objc.super(RuleTable, self).keyDown_(event)


class ReplacementsEditor(NSObject):
    """`owner` provides config (its replacements) and replace_word_rules(rules),
    which saves them and returns whether that reached the disk. `on_resize`
    is called when the note or the Try it result may have changed height."""

    def initWithOwner_width_onResize_(self, owner, width, on_resize):
        self = objc.super(ReplacementsEditor, self).init()
        if self is None:
            return None
        self.owner = owner
        # Until the editor is built and placed there is no window to fit.
        self._on_resize = lambda: None
        self.rules = []
        # What the list was before the last removal, while Undo can still bring it back.
        self._before_removal = None
        add = NSButton.buttonWithTitle_target_action_("Add", self, "addRule:")
        # Whole points, so no edge falls between pixels; Add takes what is
        # left, so its bezel ends where the list does.
        field_width = math.floor((width - aligned_width(add) - 2 * GAP) / 2)
        add.widthAnchor().constraintEqualToConstant_(width - 2 * field_width - 2 * GAP).setActive_(True)
        self.heard = self._field(_TITLES[HEARD], field_width)
        self.replacement = self._field(_TITLES[REPLACEMENT], field_width)
        add_row = stack([self.heard, self.replacement, add], horizontal=True, spacing=GAP)

        self.table = RuleTable.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
        self.table.setStyle_(NSTableViewStyleFullWidth)
        # The second column starts where the second field does, so each
        # column's text sits under the text of the field that adds to it.
        first_width = field_width + GAP - self.table.intercellSpacing().width
        for identifier in (HEARD, REPLACEMENT):
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            header = RuleHeaderCell.alloc().initTextCell_(_TITLES[identifier])
            header.setFont_(column.headerCell().font())
            column.setHeaderCell_(header)
            column.setWidth_(first_width if identifier == HEARD else width - first_width)
            self.table.addTableColumn_(column)
        self.table.setColumnAutoresizingStyle_(NSTableViewLastColumnOnlyAutoresizingStyle)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setAllowsMultipleSelection_(True)
        self.table.setAllowsColumnReordering_(False)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        self.table.setTarget_(self)
        self.table.setDoubleAction_("editRule:")
        # Cell text starts where a field's text does; measured, not assumed.
        self._cell_inset = (self.heard.cell().drawingRectForBounds_(NSMakeRect(0, 0, field_width, 22)).origin.x
                            - _CELL_TEXT_OFFSET)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
        scroll.setDocumentView_(self.table)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        rows_height = VISIBLE_ROWS * (self.table.rowHeight() + self.table.intercellSpacing().height)
        scroll.heightAnchor().constraintEqualToConstant_(
            rows_height + self.table.headerView().frame().size.height + 2).setActive_(True)
        scroll.widthAnchor().constraintEqualToConstant_(width).setActive_(True)

        self.remove = NSButton.buttonWithTitle_target_action_("Remove", self, "removeRules:")
        self.undo = NSButton.buttonWithTitle_target_action_("Undo", self, "undoRemoval:")
        self.count = NSTextField.labelWithString_("")
        self.count.setTextColor_(NSColor.secondaryLabelColor())
        list_row = stack([self.remove, self.undo, spacer(), self.count], horizontal=True, spacing=GAP)
        list_row.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        self.note = small_text("", width)

        self.trial = NSTextField.textFieldWithString_("")
        self.trial.setPlaceholderString_("Type a sentence to see your replacements at work")
        self.trial.setBezelStyle_(NSTextFieldRoundedBezel)
        self.trial.setDelegate_(self)
        self.trial.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        self.trial_result = NSTextField.wrappingLabelWithString_("")
        self.trial_result.setSelectable_(True)
        self.trial_result.setPreferredMaxLayoutWidth_(width)

        self.view = stack([add_row, scroll, list_row, self.note], spacing=6)
        self.view.setCustomSpacing_afterView_(10, add_row)
        # The sentence with replacements applied starts where the typed one does.
        result_row = stack([self.trial_result])
        result_row.setEdgeInsets_((0, self.trial.cell().drawingRectForBounds_(NSMakeRect(0, 0, width, 22)).origin.x
                                   + self.trial_result.alignmentRectInsets().left, 0, 0))
        self.trial_view = stack([self.trial, result_row], spacing=6)
        self._say(GUIDE if owner.config.replacements else EMPTY_GUIDE)
        self.refresh()
        self._on_resize = on_resize
        return self

    @objc.python_method
    def _field(self, placeholder, width):
        field = NSTextField.textFieldWithString_("")
        field.setPlaceholderString_(placeholder)
        field.setAccessibilityLabel_(placeholder)
        field.setBezelStyle_(NSTextFieldRoundedBezel)
        field.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        field.setDelegate_(self)
        # Return adds, as Add does; Tab and clicking away do not.
        field.setTarget_(self)
        field.setAction_("addRule:")
        field.cell().setSendsActionOnEndEditing_(False)
        return field

    # -- Showing the rules --

    @objc.python_method
    def refresh(self):
        """Show the saved rules, keeping the selection on the same rules."""
        rules = displayed_rules(self.owner.config.replacements)
        # Settings refreshes for other reasons too; reloading unchanged rules
        # would end a cell edit the user is in the middle of.
        if rules != self.rules or self.table.numberOfRows() != len(rules):
            selected = {rule_key(self.rules[row]["heard"]) for row in self._selected_rows()}
            self.rules = rules
            self.table.reloadData()
            self._select([index for index, rule in enumerate(self.rules) if rule_key(rule["heard"]) in selected])
            if not rules:
                self._say(EMPTY_GUIDE)
        self.count.setStringValue_(count_text(len(self.rules)))
        self.remove.setEnabled_(bool(self._selected_rows()))
        self.undo.setHidden_(self._before_removal is None)
        self._show_trial()

    @objc.python_method
    def _selected_rows(self):
        indexes = self.table.selectedRowIndexes()
        return [row for row in range(len(self.rules)) if indexes.containsIndex_(row)]

    @objc.python_method
    def _select(self, rows):
        indexes = NSIndexSet.indexSet().mutableCopy()
        for row in rows:
            indexes.addIndex_(row)
        self.table.selectRowIndexes_byExtendingSelection_(indexes, False)
        if rows:
            self.table.scrollRowToVisible_(rows[0])
        self.remove.setEnabled_(bool(rows))

    @objc.python_method
    def _say(self, text):
        self.note.setStringValue_(text)
        self._on_resize()

    @objc.python_method
    def _show_trial(self):
        text = str(self.trial.stringValue())
        self.trial_result.setStringValue_(apply_replacements(text, self.rules) if text.strip() else "")
        self.trial_result.setHidden_(not text.strip())
        self._on_resize()

    @objc.python_method
    def _store(self, rules):
        """Save `rules` in the order the list shows them; True when that reached the disk."""
        return self.owner.replace_word_rules(displayed_rules(rules))

    @objc.python_method
    def _finish_cell_edit(self):
        """A cell edit still open belongs to the list as it is now: save it
        before an action changes the list under its row number."""
        window = self.table.window()
        responder = window.firstResponder() if window is not None else None
        if (isinstance(responder, NSTextView) and responder.isFieldEditor()
                and isinstance(responder.delegate(), NSTextField) and self.table.rowForView_(responder.delegate()) >= 0):
            window.makeFirstResponder_(self.table)

    @objc.python_method
    def _save(self, rules, heard, message):
        """Save `rules` and show them, the rule for `heard` selected, with `message` under the list."""
        if not self._store(rules):
            message = "This works until Maramax quits, but settings could not be saved."
        self.refresh()
        index = find_rule(self.rules, heard)
        self._select([] if index is None else [index])
        self._say(message)

    # -- Table data --

    def numberOfRowsInTableView_(self, table):
        return len(self.rules)

    def tableView_viewForTableColumn_row_(self, table, column, row):
        identifier = str(column.identifier())
        cell = table.makeViewWithIdentifier_owner_(identifier, self) or self._cell(identifier)
        text = self.rules[row][identifier]
        cell.textField().setStringValue_(text if editable_in_place(text) else _LINE_BREAK.join(text.splitlines()))
        cell.textField().setEditable_(editable_in_place(text))
        return cell

    @objc.python_method
    def _cell(self, identifier):
        """One line of text, centred in the row, edited in place."""
        field = NSTextField.labelWithString_("")
        field.setLineBreakMode_(NSLineBreakByTruncatingTail)
        field.cell().setUsesSingleLineMode_(True)
        field.setTarget_(self)
        field.setAction_("ruleEdited:")
        field.cell().setSendsActionOnEndEditing_(True)  # Tab or a click away saves, as Return does.
        field.setTranslatesAutoresizingMaskIntoConstraints_(False)
        cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 24))
        cell.setIdentifier_(identifier)
        cell.addSubview_(field)
        cell.setTextField_(field)
        NSLayoutConstraint.activateConstraints_([
            field.leadingAnchor().constraintEqualToAnchor_constant_(cell.leadingAnchor(), self._cell_inset),
            field.trailingAnchor().constraintEqualToAnchor_constant_(cell.trailingAnchor(), -self._cell_inset),
            field.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
        ])
        return cell

    def editRule_(self, sender):
        """A double-click: edit the clicked cell where it is."""
        del sender
        row, column = self.table.clickedRow(), self.table.clickedColumn()
        if not (0 <= row < len(self.rules) and column >= 0):
            return
        identifier = str(self.table.tableColumns()[column].identifier())
        if not editable_in_place(self.rules[row][identifier]):
            self._say("This replacement has several lines. To change it, remove it and add it again.")
            return
        self.table.editColumn_row_withEvent_select_(column, row, None, True)

    def ruleEdited_(self, sender):
        """A cell's text field finished editing (Return, Tab, or a click away)."""
        row, column = self.table.rowForView_(sender), self.table.columnForView_(sender)
        if not (0 <= row < len(self.rules) and column >= 0):
            return  # The list changed under the edit.
        changed = dict(self.rules[row])
        changed[str(self.table.tableColumns()[column].identifier())] = str(sender.stringValue())
        if changed == self.rules[row]:
            return
        result = edited_rules(self.rules, changed[HEARD], changed[REPLACEMENT], at=row)
        if isinstance(result, RuleRefused):
            self._say(f"{result.reason} The change was not saved.")
            self.table.reloadData()
            return
        self._before_removal = None
        self._save(result, changed[HEARD], "Change saved.")

    def tableViewSelectionDidChange_(self, notification):
        del notification
        self.remove.setEnabled_(bool(self._selected_rows()))

    # -- Fields --

    def controlTextDidChange_(self, notification):
        field = notification.object()
        if field is self.trial:
            self._show_trial()
        elif field is self.heard:
            # Said while typing, so a rule is never entered twice by mistake.
            index = find_rule(self.rules, str(self.heard.stringValue()))
            if index is not None:
                self._select([index])
                self._say(f"“{self.rules[index]['heard']}” is already in the list. "
                          "Double-click it below to change it.")
            else:
                self._say(GUIDE if self.rules else EMPTY_GUIDE)

    # -- Actions --

    def addRule_(self, sender):
        del sender
        self._finish_cell_edit()
        heard, replacement = str(self.heard.stringValue()), str(self.replacement.stringValue())
        result = edited_rules(self.rules, heard, replacement, at=None)
        if isinstance(result, RuleRefused):
            index = find_rule(self.rules, heard)
            if index is not None:
                self._select([index])
            self._say(result.reason)
            return
        self._before_removal = None
        self.heard.setStringValue_("")
        self.replacement.setStringValue_("")
        added = result[-1]
        self._save(result, added[HEARD], f"Added: “{added[HEARD]}” becomes “{added[REPLACEMENT]}”.")
        self.heard.window().makeFirstResponder_(self.heard)

    def removeRules_(self, sender):
        del sender
        self._finish_cell_edit()
        rows = self._selected_rows()
        if not rows:
            return
        before = list(self.rules)
        removed = [before[row][HEARD] for row in rows]
        saved = self._store([rule for row, rule in enumerate(before) if row not in rows])
        self._before_removal = before
        self.refresh()
        self._select([])
        if not saved:
            self._say("Removed until Maramax quits, but settings could not be saved.")
        elif len(removed) == 1:
            self._say(f"Removed “{removed[0]}”.")
        else:
            self._say(f"Removed {len(removed)} replacements.")

    def undoRemoval_(self, sender):
        del sender
        self._finish_cell_edit()
        if self._before_removal is None:
            return  # The edit just saved replaced what Undo would have restored.
        restored, self._before_removal = self._before_removal, None
        saved = self._store(restored)
        self.refresh()
        self._say("Restored." if saved else "Restored until Maramax quits, but settings could not be saved.")
