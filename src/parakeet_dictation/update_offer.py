"""Offering newer versions of Maramax from the menu bar; updater.py does the fetching and replacing."""

from __future__ import annotations

import enum
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import rumps
from AppKit import NSApplication
from PyObjCTools import AppHelper

from . import updater
from .config import AppConfig
from .logger_config import logger
from .main_thread import call_later
from .update_prompt import Choice, UpdatePromptWindow
from .update_window import UpdateProgressWindow, download_size

CHECK_TITLE = "Check for Updates…"
# The first automatic check comes a minute after launch, out of its way.
FIRST_CHECK_SECONDS = 60
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# An install quits the app, so it waits until nothing has been running for
# this long: a finished dictation still pastes. (The outcome it shows counts
# as busy for as long as it is on screen.)
IDLE_BEFORE_INSTALL_SECONDS = 3.0
# Long enough to read "Restarting Maramax…" before the window goes.
RESTART_NOTICE_SECONDS = 0.8
# A quit that has not happened by then is not going to: the swap script has
# given up waiting for it, so saying so cannot race an install still to come.
QUIT_WATCHDOG_SECONDS = updater.QUIT_WAIT_SECONDS + 5
# A new app left staged by a run that quit before installing it is removed
# this long after launch: by then a swap still running from that quit (if
# Maramax was reopened at once) has long finished with it.
LEFTOVER_REMOVAL_SECONDS = 30


class Step(enum.Enum):
    IDLE = "idle"
    CHECKING = "checking"
    PROMPTING = "prompting"      # the Software Update window waits for an answer
    DOWNLOADING = "downloading"
    CANCELLING = "cancelling"    # Cancel pressed; the download stops, or the staged app is discarded
    INSTALLING = "installing"    # downloaded and verified; waiting to quit


@dataclass(frozen=True)
class CheckFailed:
    problem: str


# How the last check went: one value, so "failed" always carries its reason.
LastCheck = Literal["not checked", "succeeded"] | CheckFailed


def menu_title(step: Step, version: str | None, percent: int | None = None) -> str:
    """What the menu item says. `version` is the newer release, once known.
    The menu is a fixed width, so every title stays short."""
    if step is Step.CHECKING:
        return "Checking for Updates…"
    if step is Step.DOWNLOADING:
        return f"Downloading Update… {percent or 0}%"
    if step is Step.CANCELLING:
        return "Cancelling the Update…"
    if step is Step.INSTALLING:
        return f"Installing Maramax {version}…"
    return f"Install Maramax {version}…" if version else CHECK_TITLE


def status_text(*, step: Step, version: str | None, percent: int | None, last_check: LastCheck,
                updated_to: str | None = None) -> str:
    """The sentence under Settings → Advanced → Updates. `updated_to` is set on the
    first launch after an update installed."""
    if step is Step.CHECKING:
        return "Checking for updates…"
    if step is Step.DOWNLOADING:
        return f"Downloading Maramax {version}… {percent or 0}%"
    if step is Step.CANCELLING:
        return f"Cancelling the update to Maramax {version}…"
    if step is Step.INSTALLING:
        return f"Maramax {version} is ready and installs as soon as Maramax is idle."
    if version:
        return f"Maramax {version} is available."
    if isinstance(last_check, CheckFailed):
        return f"The last check did not work: {last_check.problem}"
    if updated_to:
        return f"Updated to Maramax {updated_to}."
    return "This is the newest version." if last_check == "succeeded" else "Not checked yet."


_INSTALL_FAILURES = {
    updater.InstallResult.NOT_QUIT: "Maramax did not quit in time, so nothing was changed.",
    updater.InstallResult.STAGED_MISSING: "The downloaded version was missing when it was time to install it.",
    updater.InstallResult.NOT_MOVED_ASIDE: "macOS did not let Maramax move itself aside, so it was left as it was. "
                                           "You can install the new version by hand from its release page.",
    updater.InstallResult.NOT_PLACED: "The new version could not be put in place, so the previous one was restored.",
    updater.InstallResult.NOT_RESTORED: "The new version could not be put in place, and the previous one could not "
                                        "be moved back. It is running from the hidden .Maramax-replaced.app beside "
                                        "where Maramax was: rename that to Maramax.app in Finder (Cmd+Shift+. "
                                        "shows hidden files) to keep it.",
}


def install_failure(result: updater.InstallResult) -> str | None:
    """What to tell the user about the last update's outcome; None when it worked."""
    return _INSTALL_FAILURES.get(result)


def should_prompt(*, asked: bool, version: str, skipped_version: str | None, busy: bool) -> bool:
    """A check the user asked for always answers. One that ran by itself
    stays quiet about a skipped version, and never interrupts work."""
    return asked or (version != skipped_version and not busy)


def ready_to_install(*, idle_now: bool, idle_before: bool) -> bool:
    """Idle on two looks in a row: a dictation that just finished has had
    time to paste and show its outcome."""
    return idle_now and idle_before


class UpdateOffer:
    def __init__(self, *, menu_item, current_version: str, installed_app: Path | None, support_dir: Path,
                 config: AppConfig, save_settings: Callable[[], bool], is_busy: Callable[[], bool],
                 quit_app: Callable[[], None], on_change: Callable[[], None]):
        self._menu_item = menu_item
        self._current = current_version
        # None when running from source: there is no bundle to replace.
        self._installed_app = installed_app
        self._paths = updater.UpdatePaths.under(support_dir)
        self._config = config
        self._save_settings = save_settings
        # True while quitting would interrupt something or cut it short.
        self._is_busy = is_busy
        self._quit_app = quit_app
        # Told whenever what status_text() says may have changed.
        self._on_change = on_change
        self._step = Step.IDLE
        self._percent: int | None = None
        self._release: updater.Release | None = None
        self._last_check: LastCheck = "not checked"
        self._updated_to: str | None = None  # this launch follows an update that installed
        self._cancel = threading.Event()     # one per download, so a cancel cannot outlive it
        self._window: UpdateProgressWindow | None = None
        self._prompt: UpdatePromptWindow | None = None

    def start(self) -> None:
        """Report how the last update went, then begin the automatic checks
        (each is skipped while the setting is off)."""
        self._report_last_install()
        call_later(LEFTOVER_REMOVAL_SECONDS, self._remove_leftover)
        call_later(FIRST_CHECK_SECONDS, self._scheduled_check)

    def _remove_leftover(self) -> None:
        """A new app staged by an earlier run that quit, logged out, or crashed
        while it waited to install. While IDLE, nothing of this run is staged."""
        if self._installed_app is None or self._step is not Step.IDLE:
            return
        leftover = updater.staged_app(self._installed_app)
        if leftover.exists() and leftover != self._installed_app:  # Never the copy that is running.
            logger.info(f"Removing an update that was never installed: {leftover}")
            self._discard(leftover)

    def _report_last_install(self) -> None:
        try:
            result = updater.take_install_result(self._paths.result)
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
        logger.error(f"The last update did not install ({result}): {failure} See {self._paths.log}.")
        # Once the menu bar is up, not in the middle of launching.
        call_later(5, lambda: rumps.alert(title="The update was not installed", message=failure))

    def status_text(self) -> str:
        return status_text(step=self._step, version=self._release.version if self._release else None,
                           percent=self._percent, last_check=self._last_check, updated_to=self._updated_to)

    def can_check(self) -> bool:
        """A check replaces an offer still waiting for an answer."""
        return self._step in (Step.IDLE, Step.PROMPTING)

    def check_requested(self) -> None:
        """The menu item or Settings' Check Now: ask GitHub (a release found
        earlier may since have been replaced or withdrawn), then offer."""
        if not self.can_check():
            # The title already says what is happening; the window shows more.
            if self._window is not None:
                self._window.bring_forward()
            return
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
        if self._step is Step.PROMPTING:
            # An offer left unanswered (behind another app, perhaps for days)
            # must neither go stale nor stop the checks: it is taken back,
            # and the check offers whatever GitHub says now.
            assert self._prompt is not None  # PROMPTING is set only once it exists.
            self._prompt.withdraw()
            self._set_step(Step.IDLE)
        if self._step is not Step.IDLE:
            return
        self._set_step(Step.CHECKING)
        threading.Thread(target=self._check_worker, args=(asked,), daemon=True).start()

    def _check_worker(self, asked: bool) -> None:
        try:
            release = updater.latest_release(self._current)
        except updater.UpdateError as exc:
            AppHelper.callAfter(self._check_failed, str(exc), asked)
            return
        except Exception as exc:
            # Never leave the menu stuck on "Checking": report and carry on.
            logger.exception("Update check failed unexpectedly")
            AppHelper.callAfter(self._check_failed, f"Unexpected error: {exc}", asked)
            return
        AppHelper.callAfter(self._checked, release, asked)

    def _check_failed(self, problem: str, asked: bool) -> None:
        logger.warning(f"Update check failed: {problem}")
        self._last_check = CheckFailed(problem)
        self._set_step(Step.IDLE)  # What was known before still stands.
        if asked:
            rumps.alert(title="Could not check for updates", message=problem)

    def _checked(self, release: updater.Release | None, asked: bool) -> None:
        self._release = release
        self._last_check = "succeeded"
        self._set_step(Step.IDLE)
        if release is None:
            logger.info(f"Update check: {self._current} is the newest release")
            if asked:
                rumps.alert(title="You’re up to date!",
                            message=f"Maramax {self._current} is currently the newest version available.")
            return
        logger.info(f"Maramax {release.version} is available (running {self._current})")
        # An open dialog or file panel counts as busy: the prompt would open behind it.
        if should_prompt(asked=asked, version=release.version, skipped_version=self._config.skipped_update_version,
                         busy=not self._idle()):
            self._offer(release, asked)

    # -- Offering --

    def _offer(self, release: updater.Release, asked: bool) -> None:
        """Open the Software Update window. While it waits, PROMPTING keeps
        the daily check from offering again; the answer comes to _answered."""
        if self._prompt is None:
            self._prompt = UpdatePromptWindow.alloc().initWithChoice_(self._answered)
        self._set_step(Step.PROMPTING)
        # A check that ran by itself leaves the keyboard where the user is typing.
        self._prompt.show(version=release.version, current_version=self._current, notes=release.notes,
                          size=download_size((release.delta or release.archive).size), activate=asked)

    def _answered(self, choice: Choice) -> None:
        # The window answers once per offer, and nothing else leaves PROMPTING.
        assert self._step is Step.PROMPTING, self._step
        release = self._release
        assert release is not None  # Offered releases are kept until the next check.
        self._set_step(Step.IDLE)
        if choice is Choice.INSTALL:
            self._download(release)
        elif choice is Choice.SKIP:
            self._config.skipped_update_version = release.version
            self._save_settings()

    # -- Downloading --

    def _download(self, release: updater.Release) -> None:
        installed_app = self._installed_app
        if installed_app is None:
            rumps.alert(title="This copy runs from source",
                        message=f"Update the checkout instead, or download the app from {release.page_url}")
            return
        try:
            updater.ensure_installable(installed_app, self._paths.updates)
        except updater.UpdateError as exc:
            rumps.alert(title=f"Maramax {release.version} cannot be installed here", message=str(exc))
            return
        self._release = release
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
        if self._step in (Step.DOWNLOADING, Step.INSTALLING):
            # Said at once, though a stalled read can take a while to give up
            # and the wait for idle looks again only every few seconds. Never
            # IDLE here: a new download would replace self._cancel, and the
            # look still to come would then install the app it should discard.
            self._set_step(Step.CANCELLING)
        if self._window is not None:
            self._window.close()

    def _show_download_progress(self, received: int, expected: int) -> None:
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
                AppHelper.callAfter(self._show_download_progress, received, expected)

        try:
            staged_app = updater.download(release, self._current, installed_app, self._paths.download,
                                          progress, cancel.is_set)
        except Exception as exc:
            if isinstance(exc, updater.UpdateError):
                problem = str(exc)
            else:
                # Never leave the menu stuck on "Downloading": report and carry on.
                logger.exception("Update download failed unexpectedly")
                problem = f"Unexpected error: {exc}"
            # After Cancel, whatever ended the download (UpdateCancelled, or a
            # stalled read timing out) is the stop the user asked for.
            if cancel.is_set():
                AppHelper.callAfter(self._download_stopped, release)
            else:
                AppHelper.callAfter(self._download_failed, release, problem)
            return
        AppHelper.callAfter(self._staged, release, installed_app, staged_app)

    def _download_stopped(self, release: updater.Release) -> None:
        logger.info(f"Update to {release.version} cancelled")
        self._set_step(Step.IDLE)

    def _download_failed(self, release: updater.Release, problem: str) -> None:
        logger.error(f"Update to {release.version} failed: {problem}")
        self._set_step(Step.IDLE)
        if self._window is not None:
            self._window.close()
        rumps.alert(title=f"Maramax {release.version} could not be installed",
                    message=f"{problem}\n\nThe current version keeps working; try again from the menu.")

    def _staged(self, release: updater.Release, installed_app: Path, staged_app: Path) -> None:
        if self._cancel.is_set():
            # The cancel arrived after the download had finished.
            self._download_stopped(release)
            self._discard(staged_app)
            return
        logger.info(f"Maramax {release.version} downloaded and verified: {staged_app}")
        self._set_step(Step.INSTALLING)
        self._install_when_idle(release, installed_app, staged_app, idle_before=False)

    # -- Installing --

    def _idle(self) -> bool:
        return not self._is_busy() and NSApplication.sharedApplication().modalWindow() is None

    def _install_when_idle(self, release: updater.Release, installed_app: Path, staged_app: Path,
                           idle_before: bool) -> None:
        """Look again every few seconds until the app has been idle, with no
        dialog or file panel open, on two looks in a row."""
        if self._cancel.is_set():
            logger.info("Update cancelled before it was installed")
            self._set_step(Step.IDLE)
            self._discard(staged_app)
            return
        idle = self._idle()
        if not ready_to_install(idle_now=idle, idle_before=idle_before):
            if self._window is not None:
                self._window.show_ready(release.version, busy=not idle)
            call_later(IDLE_BEFORE_INSTALL_SECONDS, self._install_when_idle, release, installed_app, staged_app,
                       idle)
            return
        if self._window is not None:
            self._window.show_restarting()
        call_later(RESTART_NOTICE_SECONDS, self._restart, release, installed_app, staged_app)

    def _restart(self, release: updater.Release, installed_app: Path, staged_app: Path) -> None:
        """Start the swap and quit, unless something began during the notice."""
        if self._cancel.is_set():
            self._install_when_idle(release, installed_app, staged_app, idle_before=False)  # Discards it.
            return
        if not self._idle():
            self._install_when_idle(release, installed_app, staged_app, idle_before=False)
            return
        try:
            updater.install_after_exit(
                staged_app=staged_app, installed_app=installed_app,
                previous_app=self._paths.previous(installed_app), staging=self._paths.download,
                result_path=self._paths.result, log_path=self._paths.log, pid=os.getpid(),
            )
        except updater.UpdateError as exc:
            logger.error(str(exc))
            self._set_step(Step.IDLE)
            self._discard(staged_app)  # A retry downloads it again.
            if self._window is not None:
                self._window.close()
            rumps.alert(title="The update could not be installed", message=str(exc))
            return
        logger.info("Quitting so the update can be installed")
        call_later(QUIT_WATCHDOG_SECONDS, self._quit_did_not_happen)
        self._quit_app()

    def _quit_did_not_happen(self) -> None:
        # Still here: the swap script has given up and changed nothing.
        logger.error("Maramax did not quit to install the update")
        try:
            # Taken now, so the next launch does not report this attempt again.
            outcome = updater.take_install_result(self._paths.result)
        except updater.UpdateError as exc:
            logger.warning(str(exc))
        else:
            if outcome is not None:
                logger.info(f"The installer gave up waiting for Maramax to quit ({outcome})")
        self._set_step(Step.IDLE)
        if self._window is not None:
            self._window.close()
        rumps.alert(title="The update was not installed",
                    message="Maramax could not quit to install it. Try again from the menu.")

    @staticmethod
    def _discard(staged_app: Path) -> None:
        def remove() -> None:
            shutil.rmtree(staged_app, onexc=lambda _function, path, exc: logger.warning(
                f"Could not remove {path} from a cancelled update: {exc}"))

        # Thousands of files: off the main thread.
        threading.Thread(target=remove, daemon=True).start()
