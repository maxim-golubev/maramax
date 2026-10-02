"""Offering newer versions of Maramax from the menu bar; updater.py does the fetching and replacing."""

from __future__ import annotations

import enum
import os
import threading
from collections.abc import Callable
from pathlib import Path

import rumps
from PyObjCTools import AppHelper

from . import updater
from .config import AppConfig
from .logger_config import logger
from .main_thread import call_later

CHECK_TITLE = "Check for Updates…"
# The first automatic check waits until launch work (the speech model) is done.
FIRST_CHECK_SECONDS = 60
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# An install quits the app, so it waits until nothing has been running for
# this long: a finished dictation still pastes and shows its outcome.
IDLE_BEFORE_INSTALL_SECONDS = 3.0
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


def should_prompt(*, asked: bool, version: str, skipped_version: str | None, busy: bool) -> bool:
    """A check the user asked for always answers. One that ran by itself
    stays quiet about a skipped version, and never interrupts work."""
    return asked or (version != skipped_version and not busy)


def release_message(release: updater.Release, current_version: str) -> str:
    notes = release.notes
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[: MAX_NOTES_CHARS - 1].rstrip() + "…"
    size = f"about {release.archive_size // (1024 * 1024)} MB" if release.archive_size else "the new version"
    message = (f"You have version {current_version}. Installing downloads {size}, replaces Maramax, "
               "and opens the new version. Your settings, history, and recordings stay as they are.")
    return f"{message}\n\n{notes}" if notes else message


class UpdateOffer:
    def __init__(self, *, menu_item, current_version: str, installed_app: Path | None, updates_dir: Path,
                 log_path: Path, config: AppConfig, save_settings: Callable[[], bool],
                 is_busy: Callable[[], bool], quit_app: Callable[[], None]):
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
        self._step = Step.IDLE
        self._release: updater.Release | None = None

    def start(self) -> None:
        """Begin the automatic checks (each is skipped while the setting is off)."""
        call_later(FIRST_CHECK_SECONDS, self._scheduled_check)

    def menu_clicked(self) -> None:
        if self._step is not Step.IDLE:
            return  # The title already says what is happening.
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
        version = self._release.version if self._release is not None else None
        self._menu_item.title = menu_title(step, version, percent)

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
            self._set_step(Step.IDLE)  # What was known before still stands.
            if asked:
                rumps.alert(title="Could not check for updates", message=problem)
            return
        self._release = release
        self._set_step(Step.IDLE)
        if release is None:
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
        choice = rumps.alert(title=f"Maramax {release.version} is available",
                             message=release_message(release, self._current),
                             ok="Install and Relaunch", cancel="Later", other="Skip This Version")
        self._set_step(Step.IDLE)
        if choice == 1:
            self._download(release)
        elif choice == -1:
            self._config.skipped_update_version = release.version
            self._save_settings()

    def _download(self, release: updater.Release) -> None:
        installed_app = self._installed_app
        if installed_app is None:
            rumps.alert(title="This copy runs from source",
                        message=f"Update the checkout instead, or download the app from {release.page_url}")
            return
        self._set_step(Step.DOWNLOADING, 0)
        threading.Thread(target=self._download_worker, args=(release, installed_app), daemon=True).start()

    def _download_worker(self, release: updater.Release, installed_app: Path) -> None:
        shown = [-1]

        def progress(fraction: float) -> None:
            percent = int(fraction * 100)
            if percent != shown[0]:
                shown[0] = percent
                AppHelper.callAfter(self._set_step, Step.DOWNLOADING, percent)

        try:
            new_app = updater.download(release, self._current, installed_app, self._updates_dir / "download", progress)
            problem = None
        except updater.UpdateError as exc:
            new_app, problem = None, str(exc)
        except Exception as exc:
            # Never leave the menu stuck on "Downloading": report and carry on.
            logger.exception("Update download failed unexpectedly")
            new_app, problem = None, f"Unexpected error: {exc}"
        AppHelper.callAfter(self._downloaded, release, installed_app, new_app, problem)

    def _downloaded(self, release: updater.Release, installed_app: Path, new_app: Path | None,
                    problem: str | None) -> None:
        if new_app is None:
            logger.error(f"Update to {release.version} failed: {problem}")
            self._set_step(Step.IDLE)
            rumps.alert(title=f"Maramax {release.version} could not be installed",
                        message=f"{problem}\n\nThe current version keeps working; try again from the menu.")
            return
        logger.info(f"Maramax {release.version} downloaded and verified: {new_app}")
        self._set_step(Step.INSTALLING)
        self._install_when_idle(installed_app, new_app, idle_before=False)

    def _install_when_idle(self, installed_app: Path, new_app: Path, idle_before: bool) -> None:
        """Quit and install once the app has been idle on two looks in a row."""
        idle = not self._is_busy()
        if not (idle and idle_before):
            call_later(IDLE_BEFORE_INSTALL_SECONDS, self._install_when_idle, installed_app, new_app, idle)
            return
        try:
            updater.install_after_exit(
                new_app=new_app, installed_app=installed_app,
                previous_app=self._updates_dir / "previous" / installed_app.name,
                staging=self._updates_dir / "download", log_path=self._log_path, pid=os.getpid(),
            )
        except updater.UpdateError as exc:
            logger.error(str(exc))
            self._set_step(Step.IDLE)
            rumps.alert(title="The update could not be installed", message=str(exc))
            return
        logger.info("Quitting so the update can be installed")
        self._quit_app()
