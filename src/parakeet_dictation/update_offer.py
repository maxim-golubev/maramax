"""Offering newer versions of Maramax from the menu bar; updater.py does the fetching and replacing."""

from __future__ import annotations

import enum
import os
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

import rumps
from AppKit import NSAlert, NSAlertFirstButtonReturn, NSAlertThirdButtonReturn, NSApplication
from PyObjCTools import AppHelper

from . import updater
from .config import AppConfig
from .logger_config import logger
from .main_thread import call_later
from .update_window import UpdateProgressWindow, download_size

CHECK_TITLE = "Check for Updates…"
# The first automatic check comes a minute after launch, out of its way.
FIRST_CHECK_SECONDS = 60
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# An install quits the app, so it waits until nothing has been running for
# this long: a finished dictation still pastes and shows its outcome.
IDLE_BEFORE_INSTALL_SECONDS = 3.0
# Long enough to read "Restarting Maramax…" before the window goes.
RESTART_NOTICE_SECONDS = 0.8
MAX_NOTES_CHARS = 600


class Step(enum.Enum):
    IDLE = "idle"
    CHECKING = "checking"
    PROMPTING = "prompting"      # the "is available" alert is open
    DOWNLOADING = "downloading"
    INSTALLING = "installing"    # downloaded and verified; waiting to quit


def menu_title(step: Step, version: str | None, percent: int | None = None) -> str:
    """What the menu item says. `version` is the newer release, once known."""
    if step is Step.CHECKING:
        return "Checking for Updates…"
    if step is Step.DOWNLOADING:
        return f"Downloading Maramax {version}… {percent or 0}%"
    if step is Step.INSTALLING:
        return f"Installing Maramax {version}…"
    return f"Install Maramax {version}…" if version else CHECK_TITLE


def status_line(*, step: Step, version: str | None, percent: int | None, checked: bool, problem: str | None,
                updated_to: str | None = None) -> str:
    """The sentence under Settings → Updates. `updated_to` is set on the
    first launch after an update installed."""
    if step is Step.CHECKING:
        return "Checking for updates…"
    if step is Step.DOWNLOADING:
        return f"Downloading Maramax {version}… {percent or 0}%"
    if step is Step.INSTALLING:
        return f"Maramax {version} is ready and installs as soon as Maramax is idle."
    if version:
        return f"Maramax {version} is available."
    if problem:
        return f"The last check did not work: {problem}"
    if updated_to:
        return f"Updated to Maramax {updated_to}."
    return "This is the newest version." if checked else "Not checked yet."


_INSTALL_FAILURES = {
    updater.InstallResult.NOT_QUIT: "Maramax did not quit in time, so nothing was changed.",
    updater.InstallResult.STAGED_MISSING: "The downloaded version was missing when it was time to install it.",
    updater.InstallResult.NOT_MOVED_ASIDE: "macOS did not let Maramax move itself aside, so it was left as it was. "
                                           "You can install the new version by hand from its release page.",
    updater.InstallResult.NOT_PLACED: "The new version could not be put in place, so the previous one was restored.",
}


def install_failure(result: updater.InstallResult) -> str | None:
    """What to tell the user about the last update's outcome; None when it worked."""
    return _INSTALL_FAILURES.get(result)


def plain_notes(markdown: str) -> str:
    """Release notes as an alert shows them: without Markdown's markup."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown)        # [label](url) -> label
    text = re.sub(r"(\*\*|__|`)", "", text)
    return re.sub(r"(?m)^\s*#+\s*", "", text)


def should_prompt(*, asked: bool, version: str, skipped_version: str | None, busy: bool) -> bool:
    """A check the user asked for always answers. One that ran by itself
    stays quiet about a skipped version, and never interrupts work."""
    return asked or (version != skipped_version and not busy)


def release_message(release: updater.Release, current_version: str) -> str:
    notes = plain_notes(release.notes)
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[: MAX_NOTES_CHARS - 1].rstrip() + "…"
    size = (release.delta or release.archive).size
    message = (f"You have version {current_version}. Installing downloads about "
               f"{download_size(size)}, replaces Maramax, and opens the new version. "
               "Your settings, history, and recordings stay as they are.")
    return f"{message}\n\n{notes}" if notes else message


class UpdateOffer:
    def __init__(self, *, menu_item, current_version: str, installed_app: Path | None, updates_dir: Path,
                 log_path: Path, config: AppConfig, save_settings: Callable[[], bool],
                 is_busy: Callable[[], bool], quit_app: Callable[[], None], on_change: Callable[[], None]):
        self._menu_item = menu_item
        self._current = current_version
        # None when running from source: there is no bundle to replace.
        self._installed_app = installed_app
        self._updates_dir = updates_dir
        self._log_path = log_path
        self._config = config
        self._save_settings = save_settings
        self._is_busy = is_busy
        self._quit_app = quit_app
        # Told whenever what status_text() says may have changed.
        self._on_change = on_change
        self._step = Step.IDLE
        self._percent: int | None = None
        self._release: updater.Release | None = None
        self._has_checked = False           # a check has succeeded since launch
        self._problem: str | None = None    # why the last check failed, until one succeeds
        self._updated_to: str | None = None  # this launch follows an update that installed
        self._cancel = threading.Event()     # one per download, so a cancel cannot outlive it
        self._window: UpdateProgressWindow | None = None

    def start(self) -> None:
        """Report how the last update went, then begin the automatic checks
        (each is skipped while the setting is off)."""
        self._report_last_install()
        call_later(FIRST_CHECK_SECONDS, self._scheduled_check)

    @property
    def _result_path(self) -> Path:
        return self._updates_dir / "last-install"

    def _report_last_install(self) -> None:
        try:
            result = updater.take_install_result(self._result_path)
        except updater.UpdateError as exc:
            logger.warning(str(exc))
            return
        if result is None:
            return
        failure = install_failure(result)
        if failure is None:
            logger.info(f"Updated to Maramax {self._current}")
            self._updated_to = self._current
            return
        logger.error(f"The last update did not install ({result}): {failure} See {self._log_path}.")
        # Once the menu bar is up, not in the middle of launching.
        call_later(5, lambda: rumps.alert(title="The update was not installed", message=failure))

    def status_text(self) -> str:
        return status_line(step=self._step, version=self._release.version if self._release else None,
                           percent=self._percent, checked=self._has_checked, problem=self._problem,
                           updated_to=self._updated_to)

    def can_check(self) -> bool:
        return self._step is Step.IDLE

    def check_requested(self) -> None:
        """The menu item or Settings' Check Now: offer what is known, else ask GitHub."""
        if self._step is not Step.IDLE:
            # The title already says what is happening; the window shows more.
            if self._window is not None:
                self._window.bring_forward()
            return
        if self._release is not None:
            self._offer(self._release)
        else:
            self._check(asked=True)

    # -- Checking --

    def _scheduled_check(self) -> None:
        call_later(CHECK_INTERVAL_SECONDS, self._scheduled_check)
        if self._config.check_for_updates:
            self._check(asked=False)

    def _set_step(self, step: Step, percent: int | None = None) -> None:
        self._step = step
        self._percent = percent
        version = self._release.version if self._release is not None else None
        self._menu_item.title = menu_title(step, version, percent)
        self._on_change()

    def _check(self, asked: bool) -> None:
        if self._step is not Step.IDLE:
            return
        self._set_step(Step.CHECKING)
        threading.Thread(target=self._check_worker, args=(asked,), daemon=True).start()

    def _check_worker(self, asked: bool) -> None:
        try:
            release, problem = updater.latest_release(self._current), None
        except updater.UpdateError as exc:
            release, problem = None, str(exc)
        except Exception as exc:
            # Never leave the menu stuck on "Checking": report and carry on.
            logger.exception("Update check failed unexpectedly")
            release, problem = None, f"Unexpected error: {exc}"
        AppHelper.callAfter(self._checked, release, problem, asked)

    def _checked(self, release: updater.Release | None, problem: str | None, asked: bool) -> None:
        if problem is not None:
            logger.warning(f"Update check failed: {problem}")
            self._problem = problem
            self._set_step(Step.IDLE)  # What was known before still stands.
            if asked:
                rumps.alert(title="Could not check for updates", message=problem)
            return
        self._release = release
        self._has_checked, self._problem = True, None
        self._set_step(Step.IDLE)
        if release is None:
            logger.info(f"Update check: {self._current} is the newest release")
            if asked:
                rumps.alert(title="Maramax is up to date", message=f"Version {self._current} is the newest release.")
            return
        logger.info(f"Maramax {release.version} is available (running {self._current})")
        if should_prompt(asked=asked, version=release.version, skipped_version=self._config.skipped_update_version,
                         busy=self._is_busy()):
            self._offer(release)

    # -- Offering and installing --

    def _offer(self, release: updater.Release) -> None:
        # The alert runs a nested event loop in which the daily timer can
        # fire; PROMPTING keeps a second check from opening a second alert.
        self._set_step(Step.PROMPTING)
        choice = self._ask(release)
        self._set_step(Step.IDLE)
        if choice == NSAlertFirstButtonReturn:
            self._download(release)
        elif choice == NSAlertThirdButtonReturn:
            self._config.skipped_update_version = release.version
            self._save_settings()

    def _ask(self, release: updater.Release) -> int:
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"Maramax {release.version} is available")
        alert.setInformativeText_(release_message(release, self._current))
        install = alert.addButtonWithTitle_("Install and Relaunch")
        later = alert.addButtonWithTitle_("Later")
        alert.addButtonWithTitle_("Skip This Version")
        # The prompt can appear while the user is typing elsewhere: Return
        # must not quit and replace the app, so it means Later.
        install.setKeyEquivalent_("")
        later.setKeyEquivalent_("\r")
        # A menu-bar app's alert would otherwise open behind other windows.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return int(alert.runModal())

    def _download(self, release: updater.Release) -> None:
        installed_app = self._installed_app
        if installed_app is None:
            rumps.alert(title="This copy runs from source",
                        message=f"Update the checkout instead, or download the app from {release.page_url}")
            return
        try:
            updater.check_installable(installed_app, self._updates_dir)
        except updater.UpdateError as exc:
            rumps.alert(title=f"Maramax {release.version} cannot be installed here", message=str(exc))
            return
        self._cancel = threading.Event()
        if self._window is None:
            self._window = UpdateProgressWindow.alloc().initWithCancel_(self.cancel_requested)
        self._window.show(release.version)
        self._set_step(Step.DOWNLOADING, 0)
        threading.Thread(target=self._download_worker, args=(release, installed_app, self._cancel),
                         daemon=True).start()

    def cancel_requested(self) -> None:
        """The progress window's Cancel: stop the download, or the install
        that is waiting for the app to be idle."""
        self._cancel.set()
        if self._window is not None:
            self._window.close()

    def _downloading(self, received: int, expected: int) -> None:
        if self._step is not Step.DOWNLOADING:
            return  # A report that arrived after the download ended.
        self._set_step(Step.DOWNLOADING, min(100, received * 100 // expected))
        if self._window is not None:
            self._window.show_progress(received, expected)

    def _download_worker(self, release: updater.Release, installed_app: Path, cancel: threading.Event) -> None:
        shown = [-1, -1]

        def progress(received: int, expected: int) -> None:
            # A report per whole percent, and the last one, keep the main thread unflooded.
            percent = received * 100 // expected
            if (percent, expected) != tuple(shown) or received >= expected:
                shown[:] = [percent, expected]
                AppHelper.callAfter(self._downloading, received, expected)

        try:
            staged_app = updater.download(release, self._current, installed_app, self._updates_dir / "download",
                                          progress, cancel.is_set)
            problem = None
        except updater.UpdateError as exc:
            staged_app, problem = None, str(exc)
        except Exception as exc:
            # Never leave the menu stuck on "Downloading": report and carry on.
            logger.exception("Update download failed unexpectedly")
            staged_app, problem = None, f"Unexpected error: {exc}"
        AppHelper.callAfter(self._downloaded, release, installed_app, staged_app, problem)

    def _downloaded(self, release: updater.Release, installed_app: Path, staged_app: Path | None,
                    problem: str | None) -> None:
        if self._cancel.is_set():
            logger.info(f"Update to {release.version} cancelled")
            self._set_step(Step.IDLE)
            if staged_app is not None:
                self._discard(staged_app)
            return
        if staged_app is None:
            if self._window is not None:
                self._window.close()
            logger.error(f"Update to {release.version} failed: {problem}")
            self._set_step(Step.IDLE)
            rumps.alert(title=f"Maramax {release.version} could not be installed",
                        message=f"{problem}\n\nThe current version keeps working; try again from the menu.")
            return
        logger.info(f"Maramax {release.version} downloaded and verified: {staged_app}")
        self._set_step(Step.INSTALLING)
        self._install_when_idle(installed_app, staged_app, idle_before=False)

    def _install_when_idle(self, installed_app: Path, staged_app: Path, idle_before: bool) -> None:
        """Quit and install once the app has been idle on two looks in a row,
        with no dialog or file panel open."""
        if self._cancel.is_set():
            logger.info("Update cancelled before it was installed")
            self._set_step(Step.IDLE)
            self._discard(staged_app)
            return
        idle = not self._is_busy() and NSApplication.sharedApplication().modalWindow() is None
        if not (idle and idle_before):
            if self._window is not None:
                self._window.show_ready(self._release.version if self._release else "", busy=not idle)
            call_later(IDLE_BEFORE_INSTALL_SECONDS, self._install_when_idle, installed_app, staged_app, idle)
            return
        try:
            updater.install_after_exit(
                staged_app=staged_app, installed_app=installed_app,
                previous_app=self._updates_dir / "previous" / installed_app.name,
                staging=self._updates_dir / "download", result_path=self._result_path,
                log_path=self._log_path, pid=os.getpid(),
            )
        except updater.UpdateError as exc:
            logger.error(str(exc))
            self._set_step(Step.IDLE)
            if self._window is not None:
                self._window.close()
            rumps.alert(title="The update could not be installed", message=str(exc))
            return
        logger.info("Quitting so the update can be installed")
        if self._window is not None:
            self._window.show_restarting()
        call_later(RESTART_NOTICE_SECONDS, self._quit_app)

    @staticmethod
    def _discard(staged_app: Path) -> None:
        # Thousands of files: off the main thread.
        threading.Thread(target=shutil.rmtree, args=(staged_app,), kwargs={"ignore_errors": True},
                         daemon=True).start()
