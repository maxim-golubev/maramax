"""The transcript window, dictation bar, Recordings, and the update windows, built off-screen in a separate process."""

import subprocess
import sys

HEADER = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from AppKit import NSApplication, NSApplicationActivationPolicyProhibited, NSMakeRange
NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
calls = []
delegate = SimpleNamespace(
    is_busy=False,
    queue_remove_file=lambda file_id: calls.append(("remove", file_id)),
    queue_move_file=lambda file_id, index: calls.append(("move", file_id, index)),
    transcribe_file_directly=lambda path: calls.append(("file", path)),
    queue_add_files=lambda paths: calls.append(("queue", paths)),
    dismiss_requested=lambda: calls.append("dismiss"),
)
def visible_rect(view):
    return view.alignmentRectForFrame_(view.frame())
'''


def run(body, *args):
    result = subprocess.run([sys.executable, "-c", HEADER + body, *args], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1500:]


def test_queue_buttons_act_only_on_a_file_the_user_can_see_selected():
    run(r'''
from parakeet_dictation.overlay import Mode, OverlayController
from parakeet_dictation.file_queue import QueuedFile, QueueStatus
window = OverlayController.alloc().initWithDelegate_(delegate)
window._set_mode(Mode.QUEUE)
files = [QueuedFile(id=name, path="/x/" + name, filename=name + ".m4a") for name in ("a", "b", "c")]
window.set_queue_files(files)
assert not window.queue_remove_button.isEnabled()  # Nothing chosen yet.
window.queueRemove_(None)
window.queueMoveUp_(None)
assert calls == []                                  # In particular, not the last file.
text = str(window.queue_text_view.string())
window.queue_text_view.setSelectedRange_(NSMakeRange(text.index("b.m4a"), 0))  # A click on the second line.
selected = window.queue_text_view.selectedRange()
assert text[selected.location:selected.location + selected.length].strip() == "2. b.m4a"  # Shown as a selected line.
assert window.queue_remove_button.isEnabled()
window.queueMoveDown_(None)
assert calls == [("move", "b", 2)]
window.set_queue_files([files[0], files[2], files[1]])      # The choice follows the file, not the line.
assert window._selected_queue_index() == 2
window.queueRemove_(None)
assert calls[-1] == ("remove", "b")
window.set_queue_files([files[0], files[2]])
assert window._selected_queue_index() is None and not window.queue_remove_button.isEnabled()
many = [QueuedFile(id=str(n), path=f"/x/{n}", filename=f"{n}.wav") for n in range(12)]
many[3].status = QueueStatus.PROCESSING
window.set_queue_files(many)
lines = str(window.queue_text_view.string()).split("\n")
assert lines[0].index(". ") == lines[11].index(". ")   # Numbers are right-aligned: " 1." above "12.".
assert "transcribing…" in lines[3]
# AppKit counts in UTF-16, where an emoji is two units: a click after one must still land on its own line.
emoji = [QueuedFile(id="memo", path="/x/memo", filename="Voice memo 🎤.m4a"), QueuedFile(id="other", path="/x/o", filename="other.wav")]
window.set_queue_files(emoji)
first_line = window.queue_text_view.string().componentsSeparatedByString_("\n")[0]
window.queue_text_view.setSelectedRange_(NSMakeRange(first_line.length(), 0))   # A click at the end of line 1.
assert window._selected_queue_index() == 0
window.queueRemove_(None)
assert calls[-1] == ("remove", "memo")
window._selected_queue_file_id = "other"
window.set_queue_files(emoji)
chosen = window.queue_text_view.selectedRange()
assert str(window.queue_text_view.string().substringWithRange_(chosen)).strip() == "2. other.wav"
window.queue_text_view.setSelectedRange_(NSMakeRange(0, 5))   # Dragging out text to copy chooses nothing new.
assert window._selected_queue_index() == 1
''')


def test_transcript_window_edges_line_up_in_every_state():
    run(r'''
from parakeet_dictation.overlay import BOTTOM, MARGIN, Mode, OverlayController, WIDTH
from parakeet_dictation.file_queue import QueuedFile
window = OverlayController.alloc().initWithDelegate_(delegate)
right = WIDTH - MARGIN

def check(state):
    shown = [v for v in window._all_views if not v.isHidden()]
    rects = {id(v): visible_rect(v) for v in shown}
    # Centred text has no edge to line up; controls and text areas do.
    labels = {id(window.status_label), id(window.detail_label), id(window.drop_label)}
    edges = [r for key, r in rects.items() if key not in labels]
    if state != "transcribing":  # That state is a status line over one centred button.
        assert abs(min(r.origin.x for r in edges) - MARGIN) < 0.6, (state, [r.origin.x for r in edges])
        assert abs(max(r.origin.x + r.size.width for r in edges) - right) < 0.6, state
    assert abs(min(r.origin.y for r in rects.values()) - BOTTOM) < 2.1, (state, [r.origin.y for r in rects.values()])
    # Controls that share a row share a centre line.
    row = [window.mode_control, window.record_button, window.copy_button, window.files_button, window.close_button]
    centres = {round(rects[id(v)].origin.y + rects[id(v)].size.height / 2, 1) for v in row if id(v) in rects}
    assert len(centres) <= 1 or state in ("queue processing", "transcribing"), (state, centres)

window.set_status("Ready"); check("idle")
window.set_intro_text("Welcome"); check("intro")
assert not window.copy_button.isEnabled()          # Guidance is not a transcript.
window.set_recording(True); check("recording")
window.set_current_text("Live draft"); check("recording with draft")
window.set_recording(False); window.set_current_text("A finished transcript."); check("result")
assert window.copy_button.isEnabled()
window.set_transcribing(True); check("transcribing")
window.set_transcribing(False)
window._set_mode(Mode.QUEUE)
window.set_queue_files([QueuedFile(id=str(n), path=f"/x/{n}", filename=f"{n}.wav") for n in range(12)]); check("queue")
tabs = visible_rect(window.mode_control)
assert tabs.size.width >= window.mode_control.cell().cellSize().width - 0.5   # "Queue (12)" is not squeezed.
window.set_queue_processing(True); window.set_transcribing(True); check("queue processing")

long_status = "Speech model unavailable — check your connection, then retry the speech model from the menu, please"
window.set_status(long_status)
assert window.status_label.cell().usesSingleLineMode() and str(window.status_label.toolTip()) == long_status
window.set_transcribing(False); window.set_queue_processing(False)
from parakeet_dictation.overlay import DROP_HINT, DropTarget, drop_target
window.set_drop_state(window.drop_target(1))
assert str(window.status_label.stringValue()) == "Drop to add to the queue"   # In Queue mode, even one file.
window.set_drop_state(None)
assert str(window.status_label.stringValue()) == long_status
window._set_mode(Mode.HISTORY)
window.set_drop_state(window.drop_target(1))               # Also where the hint label is not on screen.
assert str(window.status_label.stringValue()) == "Drop to transcribe"
window.set_drop_state(window.drop_target(3))               # Several files are queued, wherever they land.
assert str(window.status_label.stringValue()) == "Drop to add to the queue"
window.set_drop_state(None)
# What a drop does: one rule for the feedback and for the drop itself.
assert drop_target(Mode.RESULT, 1, busy=False) is DropTarget.TRANSCRIBE
assert drop_target(Mode.RESULT, 2, busy=False) is DropTarget.QUEUE
assert drop_target(Mode.RESULT, 1, busy=True) is DropTarget.QUEUE      # Recording or transcribing: it waits in the queue.
assert drop_target(Mode.QUEUE, 1, busy=False) is DropTarget.QUEUE
assert "several to add them to the queue" in DROP_HINT
window.hide()
assert str(window.status_label.stringValue()) == long_status  # Reopening shows the status, not a blank.
assert not window.panel.isVisible()
''')


def test_dictation_bar_geometry_and_two_line_outcomes():
    run(r'''
from parakeet_dictation.indicator import DictationIndicator, FINISHED_HINT, HEIGHT, split_status
bar = DictationIndicator.alloc().initWithDelegate_(delegate)
stop, expand = bar.stop_button.frame(), bar.expand_button.frame()
assert stop.size.width == stop.size.height == expand.size.width == expand.size.height
assert stop.origin.y == expand.origin.y == (HEIGHT - stop.size.height) / 2
from parakeet_dictation.indicator import HEALTH_STATUS
bar._layout_text(True)
for status in HEALTH_STATUS.values():     # Said beside the meter while recording: never cut short.
    bar.title.setStringValue_(status)
    assert bar.title.fittingSize().width <= bar.title.frame().size.width, status
assert split_status("Copied transcript to clipboard") == ("Copied transcript to clipboard", FINISHED_HINT)
assert split_status("No speech detected — audio saved in Recordings") == ("No speech detected", "Audio saved in Recordings")
assert split_status("Microphone unavailable: Selected microphone disconnected: AirPods") == (
    "Microphone unavailable", "Selected microphone disconnected: AirPods")
# Whichever separator comes first ends the outcome line.
assert split_status("Microphone unavailable: Connection timed out — try again") == (
    "Microphone unavailable", "Connection timed out — try again")
assert split_status("Microphone changed — now using MacBook Pro Microphone") == (
    "Microphone changed", "Now using MacBook Pro Microphone")
from parakeet_dictation import indicator
timers = []
indicator.call_later = lambda delay, fn, *args: timers.append((delay, args))
bar.finish("Copied transcript to clipboard", duration=2)
bar.finish("Copied, not pasted — you switched apps", duration=8)   # A later outcome restarts the countdown.
assert str(bar.title.stringValue()) == "Copied, not pasted" and "switched apps" in str(bar.detail.stringValue())
assert timers[0][1] != timers[1][1]            # The first timer's token is stale.
bar._hide_if_current(*timers[0][1])
assert bar._finished and timers[1][0] == 8
assert not bar.panel.isVisible() and not bar.panel.canBecomeKeyWindow()
''')


def test_recordings_window_stays_put_and_names_its_limits(tmp_path):
    run(r'''
from parakeet_dictation.recordings import DEFAULT_RECORDINGS, RecordingStore
from parakeet_dictation.recordings_window import MARGIN, WIDTH, RecordingsController
store = RecordingStore(Path(sys.argv[1]))
record = store.save(b"\x01\x00" * 16000)
window = RecordingsController.alloc().initWithDelegate_store_(delegate, store)
window.refresh()
assert not window.panel.hidesOnDeactivate()
assert str(window.panel.title()) == "Recordings"
assert f"Up to {DEFAULT_RECORDINGS} recordings" in str(window.note.stringValue())
store.limit = 50                                     # Chosen in Settings while the window is open.
window.refresh()
assert "Up to 50 recordings" in str(window.note.stringValue())
assert "not transcribed yet" in str(window.picker.titleOfSelectedItem())
for control in (window.picker, window.play):
    assert abs(visible_rect(control).origin.x - MARGIN) < 0.6
for control in (window.picker, window.retry):
    rect = visible_rect(control)
    assert abs(rect.origin.x + rect.size.width - (WIDTH - MARGIN)) < 0.6
delegate.is_busy = True
window.refresh()
assert not window.play.isEnabled() and not window.retry.isEnabled() and window.save.isEnabled()
delegate.is_busy = False
window.show_busy_state()                             # Told when the app becomes idle, not only on a refresh.
assert window.play.isEnabled() and window.retry.isEnabled()
window.sound = SimpleNamespace(isPlaying=lambda: True, stop=lambda: None)
delegate.is_busy = True                              # A dictation starts while a recording plays:
window.show_busy_state()
assert window.play.isEnabled() and not window.retry.isEnabled()   # Stop still works.
window.stop_playback()
assert not window.play.isEnabled()
delegate.is_busy = False
import threading
done = threading.Event()
window._saving = [threading.Thread(target=done.wait), threading.Thread(target=lambda: None)]
for thread in window._saving:
    thread.start()
window._saving[1].join()
assert window.is_saving()                            # The first copy still runs: quitting would cut it short.
done.set()
window._saving[0].join()
assert not window.is_saving()
''', str(tmp_path))


def test_delayed_callbacks_fire_while_a_modal_dialog_is_open():
    run(r'''
import time
from Foundation import NSDate, NSRunLoop
from parakeet_dictation.main_thread import call_later
fired = []
call_later(0.05, fired.append, "meter tick")
loop = NSRunLoop.mainRunLoop()
end = time.monotonic() + 1
while not fired and time.monotonic() < end:
    loop.runMode_beforeDate_("NSModalPanelRunLoopMode", NSDate.dateWithTimeIntervalSinceNow_(0.02))
assert fired == ["meter tick"]
''')


def test_the_bar_glyphs_sit_centred_in_their_circles():
    run(r'''
from AppKit import NSAppearance, NSMakeRect
from parakeet_dictation.indicator import Glyph, RoundIconButton
for kind in (Glyph.EXPAND, Glyph.CLOSE):
    button = RoundIconButton.alloc().initWithFrame_(NSMakeRect(0, 0, 30, 30))
    button.set_kind(kind, "x")
    button.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))
    rep = button.bitmapImageRepForCachingDisplayInRect_(button.bounds())
    button.cacheDisplayInRect_toBitmapImageRep_(button.bounds(), rep)
    w, h = rep.pixelsWide(), rep.pixelsHigh()
    # The disc is about 10 % opaque; the glyph's ink is nearly solid.
    ink = [(x, y, rep.colorAtX_y_(x, y).alphaComponent()) for y in range(h) for x in range(w)
           if rep.colorAtX_y_(x, y).alphaComponent() > 0.5]
    xs, ys = [p[0] for p in ink], [p[1] for p in ink]
    total = sum(p[2] for p in ink)
    outline = ((min(xs) + max(xs) + 1) / 2 - w / 2, (min(ys) + max(ys) + 1) / 2 - h / 2)
    weight = (sum(p[0] * p[2] for p in ink) / total + 0.5 - w / 2, sum(p[1] * p[2] for p in ink) / total + 0.5 - h / 2)
    # Within a pixel of the centre (half a point on this 2x render), both ways.
    assert all(abs(v) <= 1.0 for v in outline), (kind, outline)
    assert all(abs(v) <= 1.3 for v in weight), (kind, weight)
''')


def test_transcript_window_background_follows_light_and_dark():
    run(r'''
import warnings
from AppKit import NSAppearance, NSColor
from parakeet_dictation.overlay import OverlayController
warnings.simplefilter("ignore")  # Reading a layer's CGColor back.
window = OverlayController.alloc().initWithDelegate_(delegate)

def red():
    color = NSColor.colorWithCGColor_(window.content_view.layer().backgroundColor())
    return color.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace").redComponent()

# AppKit reports the change while drawing in the old appearance, which is the
# Mac's own here: one of these differs from it, whichever the Mac uses.
for name, dark in (("NSAppearanceNameDarkAqua", True), ("NSAppearanceNameAqua", False),
                   ("NSAppearanceNameDarkAqua", True)):
    window.panel.setAppearance_(NSAppearance.appearanceNamed_(name))
    window.content_view.viewDidChangeEffectiveAppearance()
    assert (red() < 0.5) == dark, (name, red())
''')


def test_transcript_window_never_covers_a_maramax_window_or_dialog_in_use():
    run(r'''
from AppKit import (NSApplicationDidResignActiveNotification, NSBackingStoreBuffered, NSMakeRect,
                    NSModalPanelWindowLevel, NSNormalWindowLevel, NSPanel, NSWindowDidBecomeKeyNotification,
                    NSWindowStyleMaskTitled)
from Foundation import NSNotificationCenter
from parakeet_dictation.overlay import OverlayController
window = OverlayController.alloc().initWithDelegate_(delegate)
level = window.panel.level
# Above other apps' windows; alerts and file panels open above it even when
# they are not key (an alert from the menu bar while another app is active).
assert NSNormalWindowLevel < level() < NSModalPanelWindowLevel
settings = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(0, 0, 200, 100), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
center = NSNotificationCenter.defaultCenter()
center.postNotificationName_object_(NSWindowDidBecomeKeyNotification, settings)
assert level() == NSNormalWindowLevel                # Settings, Recordings, a dialog: in front of it.
center.postNotificationName_object_(NSWindowDidBecomeKeyNotification, window.panel)
assert level() > NSNormalWindowLevel                 # Clicked again: it floats again.
center.postNotificationName_object_(NSWindowDidBecomeKeyNotification, settings)
center.postNotificationName_object_(NSApplicationDidResignActiveNotification, NSApplication.sharedApplication())
assert level() > NSNormalWindowLevel                 # In another app it floats over that app's windows.
assert not window.panel.isVisible() and not settings.isVisible()
''')


def test_window_shortcuts_follow_what_the_layout_types_with_cmd():
    run(r'''
from AppKit import NSEvent, NSEventModifierFlagCommand, NSEventTypeKeyDown
from parakeet_dictation.overlay import OverlayController
delegate.toggle_recording_requested = lambda: calls.append("toggle")
delegate.copy_current_transcript = lambda: calls.append("copy")
window = OverlayController.alloc().initWithDelegate_(delegate)

def key(typed, unmodified, code, flags=NSEventModifierFlagCommand):
    event = NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        NSEventTypeKeyDown, (0, 0), flags, 0, 0, None, typed, unmodified, False, code)
    return window.panel.performKeyEquivalent_(event)

# Russian: these keys type к, ц, с alone and r, w, c with Cmd held.
assert key("r", "к", 0x0F) and key("w", "ц", 0x0D) and key("c", "с", 0x08)
assert calls == ["toggle", "dismiss", "copy"]
assert not key("z", "z", 0x0D)       # AZERTY: Cmd+Z on the US W key is Undo, never Close.
assert key("\x1b", "\x1b", 0x35, 0) and calls[-1] == "dismiss"
''')


def test_work_that_waits_for_a_recording_is_not_offered_during_one():
    run(r'''
from AppKit import NSPasteboard, NSURL
from parakeet_dictation.file_queue import QueuedFile
from parakeet_dictation.overlay import Mode, OverlayController
window = OverlayController.alloc().initWithDelegate_(delegate)
pasteboard = NSPasteboard.pasteboardWithUniqueName()  # Not the clipboard.
pasteboard.writeObjects_([NSURL.fileURLWithPath_("/x/a.m4a"), NSURL.fileURLWithPath_("/x/b.m4a")])
drag = SimpleNamespace(draggingPasteboard=lambda: pasteboard)
try:
    assert window.content_view.draggingEntered_(drag) and window.files_button.isEnabled()
    window.content_view.draggingExited_(drag)
    window.set_recording(True)
    assert not window.content_view.draggingEntered_(drag) and not window.files_button.isEnabled()
    window._set_mode(Mode.QUEUE)
    window.set_queue_files([QueuedFile(id="a", path="/x/a.m4a", filename="a.m4a")])
    assert window.queue_add_button.isEnabled()          # Files can be collected meanwhile,
    assert not window.queue_start_button.isEnabled()    # and run once the recording ends.
    window.set_recording(False)
    assert window.queue_start_button.isEnabled()
finally:
    pasteboard.releaseGlobally()
''')


def test_a_window_left_on_a_display_that_is_gone_comes_back():
    run(r'''
from AppKit import NSMakeRect
from parakeet_dictation.overlay import OverlayController
window = OverlayController.alloc().initWithDelegate_(delegate)
window._place_on_a_screen()
frame = window.panel.frame()
visible = window.panel.screen().visibleFrame()
moved = NSMakeRect(visible.origin.x + 10, visible.origin.y + 10, frame.size.width, frame.size.height)
window.panel.setFrame_display_(moved, False)
window._place_on_a_screen()
assert window.panel.frame() == moved                 # Where the user left it.
window.panel.setFrame_display_(NSMakeRect(100000, 100000, frame.size.width, frame.size.height), False)
assert window.panel.screen() is None                 # On a display that was unplugged.
window._place_on_a_screen()
assert window.panel.screen() is not None
assert not window.panel.isVisible()
''')


def test_the_software_update_window_answers_once_and_closing_it_means_later():
    run(r'''
from parakeet_dictation.update_prompt import Choice, NO_NOTES, UpdatePromptWindow, note_blocks, rendered_notes
answers = []
window = UpdatePromptWindow.alloc().initWithChoice_(answers.append)
assert not window.panel.isVisible() and str(window.panel.title()) == "Software Update"
# Return installs and Esc is Remind Me Later, once the window has the keyboard (an
# automatic offer never takes it: show(activate=False) only orders it in front).
assert str(window.install.keyEquivalent()) == "\r" and str(window.later.keyEquivalent()) == "\x1b"
window._waiting = True                      # As show() leaves it, without putting it on screen.
window.installUpdate_(None)
window.remindLater_(None)                   # The offer was answered already.
window.windowWillClose_(None)
assert answers == [Choice.INSTALL]
window._waiting = True
window.windowWillClose_(None)               # The close button or Cmd+W.
assert answers == [Choice.INSTALL, Choice.LATER]
window._waiting = True
window.skipVersion_(None)
assert answers[-1] is Choice.SKIP and not window.panel.isVisible()
text = rendered_notes(note_blocks("## New\n- **Bold** and [a link](https://x.test)\n\nDone."))
assert str(text.string()) == "New\n\u2022\tBold and a link\nDone."
link = text.attribute_atIndex_effectiveRange_("NSLink", str(text.string()).index("a link"), None)[0]
assert str(link.absoluteString()) == "https://x.test"
assert str(rendered_notes([]).string()) == NO_NOTES
bad = rendered_notes(note_blocks("[bad](https://x.test/a|b)"))   # A URL macOS cannot parse: plain text.
assert str(bad.string()) == "bad" and bad.attribute_atIndex_effectiveRange_("NSLink", 0, None)[0] is None
''')


def test_the_progress_window_cancel_ends_where_the_bar_does():
    run(r'''
from parakeet_dictation.update_window import UpdateProgressWindow
window = UpdateProgressWindow.alloc().initWithCancel_(lambda: None)
cancel, bar = visible_rect(window.cancel), visible_rect(window.bar)
assert abs((cancel.origin.x + cancel.size.width) - (bar.origin.x + bar.size.width)) < 0.5, (cancel, bar)
''')


def test_the_bar_and_the_window_say_dont_speak_until_the_microphone_delivers_sound():
    run(r'''
from AppKit import NSColor
from parakeet_dictation.capture import CaptureMeter
from parakeet_dictation.indicator import DictationIndicator, METER_BARS, wave_levels
from parakeet_dictation.overlay import OverlayController
clock = [0.0]
meter = CaptureMeter(clock=lambda: clock[0])
meter.device_name = "AirPods"
meter.mark_open()
bar = DictationIndicator.alloc().initWithDelegate_(delegate)
window = OverlayController.alloc().initWithDelegate_(delegate)
orange = NSColor.systemOrangeColor()
bar.show("Option+Space", None)                                   # The microphone is not open yet.
window.prepare_for_recording()
assert bar.title.textColor() == orange and window.status_label.textColor() == orange
assert bar.meter.wave is not None                          # The meter is the waiting wave, not levels.
from parakeet_dictation.indicator import WAIT_TO_SPEAK_STATUS
bar.set_status(WAIT_TO_SPEAK_STATUS)                       # Read in full, never cut short.
assert bar.title.cell().cellSize().width <= bar.title.frame().size.width, bar.title.cell().cellSize()
for _ in range(3):                                         # AirPods send exact zeros while they connect.
    clock[0] += 0.15
    meter.feed(bytes(1024))
    snapshot = meter.snapshot()
    assert snapshot.health == "waiting"
    phase = bar.meter.wave
    bar.set_capture(snapshot)
    window.set_capture(snapshot)
    assert bar.meter.wave > phase                          # The wave moves on: alive, not stuck.
    assert bar.title.textColor() == orange and window.status_label.textColor() == orange
assert str(bar.detail.stringValue()) == "AirPods · 0:00" == str(window.detail_label.stringValue())
clock[0] += 0.15
meter.feed(b"\x10\x20" * 512)                              # Sound: speak now.
bar.set_capture(meter.snapshot())
window.set_capture(meter.snapshot())
assert bar.meter.wave is None and bar.title.textColor() == NSColor.labelColor()
assert window.status_label.textColor() == NSColor.labelColor()
bar.show("Option+Space", None)
bar.set_transcribing()
assert bar.title.textColor() == NSColor.labelColor()       # The colour never carries over.
bar.show("Option+Space", None)
bar.finish("Done", 2)
assert bar.title.textColor() == NSColor.labelColor()
levels = wave_levels(0.0)
assert len(levels) == METER_BARS and all(0.25 <= level <= 0.55 for level in levels)
assert levels != wave_levels(0.9)
''')


def test_the_bar_opens_where_it_was_left_and_whole_on_any_screen():
    from AppKit import NSMakeRect

    from parakeet_dictation.indicator import DEFAULT_BOTTOM, HEIGHT, SNAP_DISTANCE, WIDTH, bar_origin, placement_at

    screen = NSMakeRect(100, 50, 1440, 875)
    default = bar_origin(screen, None)
    assert default == (100 + (1440 - WIDTH) / 2, 50 + DEFAULT_BOTTOM)          # Centred near the bottom.
    assert placement_at(screen, (default[0] + SNAP_DISTANCE - 1, default[1])) is None  # Dropped beside it: back.
    left_top = placement_at(screen, (100, 50 + 875 - HEIGHT))
    assert left_top == (0.0, 1.0) and bar_origin(screen, left_top) == (100, 50 + 875 - HEIGHT)
    assert placement_at(screen, (-500, 5000)) == (0.0, 1.0)                    # Off the screen: kept on it.
    middle = placement_at(screen, (400, 300))
    assert bar_origin(screen, middle) == (400, 300)                            # Read back where it was left.
    small = NSMakeRect(0, 0, 1024, 600)                                        # Another display: still whole.
    x, y = bar_origin(small, (1.0, 1.0))
    assert x + WIDTH == 1024 and y + HEIGHT == 600


def test_the_bar_is_dragged_by_anything_but_its_buttons():
    run(r'''
from AppKit import NSPoint, NSScreen
from parakeet_dictation.indicator import DictationIndicator, bar_origin
moves = []
delegate = SimpleNamespace(bar_moved=moves.append, dismiss_requested=lambda: None)
bar = DictationIndicator.alloc().initWithDelegate_(delegate)
bar.begin("Option+Space")
bar.place(None)
visible = NSScreen.mainScreen().visibleFrame()
start = bar.panel.frame().origin
assert (start.x, start.y) == bar_origin(visible, None)
view = bar.panel.contentView()
centre = lambda control: NSPoint(control.frame().origin.x + 5, control.frame().origin.y + 5)
assert view.hitTest_(centre(bar.title)) is view and view.hitTest_(centre(bar.meter)) is view
assert view.hitTest_(centre(bar.stop_button)) is bar.stop_button
view.begin_drag(NSPoint(500, 100))
view.end_drag()                                            # A click that does not move: nothing to remember.
assert moves == []
view.begin_drag(NSPoint(500, 100))
view.drag_to(NSPoint(400, 300))
moved = bar.panel.frame().origin
assert (moved.x, moved.y) == (start.x - 100, start.y + 200)
view.end_drag()
assert len(moves) == 1 and moves[0] is not None
landed = bar.panel.frame().origin
assert (landed.x, landed.y) == bar_origin(visible, moves[0])
view.begin_drag(NSPoint(0, 0))                             # Back beside its default place: it snaps there.
view.drag_to(NSPoint(start.x - landed.x + 3, start.y - landed.y - 2))
view.end_drag()
assert moves[-1] is None and (bar.panel.frame().origin.x, bar.panel.frame().origin.y) == (start.x, start.y)
''')


def test_the_welcome_demonstration_plays_a_whole_dictation_on_a_bar_that_takes_no_clicks():
    run(r'''
from AppKit import NSPoint
from parakeet_dictation import bar_demo
from parakeet_dictation.indicator import COPIED_STATUS, HEALTH_STATUS, UPDATE_SECONDS, WAIT_TO_SPEAK_STATUS
from parakeet_dictation.capture import CaptureHealth
scheduled = []
bar_demo.call_later = lambda delay, function, *args: scheduled.append((delay, function, args))
demonstration = bar_demo.Demonstration()
bar = demonstration._bar
assert not bar.panel.isVisible() and demonstration.view.hitTest_(NSPoint(40, 40)) is None
frames = bar_demo.dictation("Control+Shift+Space")
seen = []
demonstration.start("Control+Shift+Space")
for _ in range(len(frames)):
    seen.append(str(bar.title.stringValue()))
    delay, function, args = scheduled.pop(0)
    assert delay == UPDATE_SECONDS
    function(*args)
assert seen[0] == WAIT_TO_SPEAK_STATUS and HEALTH_STATUS[CaptureHealth.RECEIVING] in seen
assert "Transcribing…" in seen and seen[-1] == COPIED_STATUS.split(" — ")[0]
assert str(bar.title.stringValue()) == WAIT_TO_SPEAK_STATUS            # Then it starts over.
assert "Control+Shift+Space" in str(bar.detail.stringValue())
demonstration.stop()
function, args = scheduled.pop(0)[1:]
function(*args)
assert scheduled == []                                                  # Stopped: no more frames.
''')


def test_a_queue_line_is_one_line_whatever_its_error_or_name_says():
    run(r'''
from parakeet_dictation.file_queue import QueuedFile, QueueStatus
from parakeet_dictation.overlay import Mode, OverlayController
window = OverlayController.alloc().initWithDelegate_(delegate)
window._set_mode(Mode.QUEUE)
window.set_queue_files([
    QueuedFile("a", "/a.wav", "a.wav", QueueStatus.FAILED, error="Unexpected error\nsecond line"),
    QueuedFile("b", "/b\nc.wav", "b\nc.wav"), QueuedFile("c", "/c.wav", "c.wav")])
lines = str(window.queue_text_view.string()).split("\n")
assert len(lines) == 3, lines
start = len(lines[0]) + 1 + len(lines[1]) + 1                 # A click on the third line.
window.queue_text_view.setSelectedRange_(NSMakeRange(start + 2, 0))
window.textViewDidChangeSelection_(SimpleNamespace(object=lambda: window.queue_text_view))
window.queueRemove_(None)
assert calls[-1] == ("remove", "c"), calls
''')


def test_a_web_address_dragged_along_with_a_file_is_not_queued():
    run(r'''
from AppKit import NSPasteboard, NSURL
from parakeet_dictation.overlay import OverlayController
window = OverlayController.alloc().initWithDelegate_(delegate)
board = NSPasteboard.pasteboardWithUniqueName()
board.clearContents()
board.writeObjects_([NSURL.fileURLWithPath_("/tmp/real.wav"), NSURL.URLWithString_("https://host/podcast/episode.mp3")])
dragged = SimpleNamespace(draggingPasteboard=lambda: board)
assert window.content_view._dragged_media(dragged) == ["/tmp/real.wav"]
board.releaseGlobally()
''')
