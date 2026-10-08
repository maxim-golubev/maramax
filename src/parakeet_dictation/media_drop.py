"""The media files a drag carries: files already on disk, or files the app they come from promises to write."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import objc
from AppKit import NSFilePromiseReceiver
from Foundation import NSURL, NSOperationQueue

from .logger_config import logger
from .main_thread import call_later

MEDIA_EXTENSIONS = [
    "aac", "aiff", "flac", "m4a", "mov", "mp3", "mp4", "ogg", "opus", "wav", "webm",
]
_FILE_URL = "public.file-url"
# What a promised file must be for the drag to be taken: sound or video of any
# kind. Its name is not known until it is written, so it cannot be held to
# MEDIA_EXTENSIONS; FFmpeg says whether it can be read.
_MEDIA_TYPE = "public.audiovisual-content"
# How long the app a file is dragged out of gets to write it (Voice Memos
# exports a long recording in a few seconds).
PROMISE_SECONDS = 60


def is_media(path: str) -> bool:
    return "." in path and path.rsplit(".", 1)[-1].lower() in MEDIA_EXTENSIONS


def drag_types() -> list[str]:
    """What a view or window registers for to be offered these drags."""
    return [_FILE_URL, *NSFilePromiseReceiver.readableDraggedTypes()]


def _files(pasteboard) -> list[str]:
    """The media files on disk among what is dragged."""
    urls = pasteboard.readObjectsForClasses_options_([NSURL], None) or []
    # A web address dragged along has a path too, of a file that is not here.
    return [url.path() for url in urls if url.isFileURL() and url.path() and is_media(url.path())]


def _promises(pasteboard) -> list:
    """The promises of a media file among what is dragged (a recording
    dragged out of Voice Memos is one: it is written only when asked for)."""
    content_type = objc.lookUpClass("UTType")
    media = content_type.typeWithIdentifier_(_MEDIA_TYPE)

    def promises_media(receiver) -> bool:
        types = [content_type.typeWithIdentifier_(identifier) for identifier in receiver.fileTypes()]
        return any(found is not None and found.conformsToType_(media) for found in types)

    receivers = pasteboard.readObjectsForClasses_options_([NSFilePromiseReceiver], None) or []
    return [receiver for receiver in receivers if promises_media(receiver)]


def offered_count(pasteboard) -> int:
    """How many media files dropping this drag would deliver; 0 for a drag that carries none."""
    return len(_files(pasteboard)) or len(_promises(pasteboard))


def received_folder(support_dir: Path) -> Path:
    """Where promised files are written: copies Maramax asked for, kept until
    the next launch (the queue that names them does not outlast the app)."""
    return support_dir / "dropped"


def is_received(path: str, support_dir: Path) -> bool:
    """Whether `path` is a copy receive() asked for: it has no original on disk to save anything beside."""
    return received_folder(support_dir) in Path(path).parents


def discard_received(support_dir: Path) -> None:
    """Delete every copy receive() asked for."""
    folder = received_folder(support_dir)
    if folder.exists():
        shutil.rmtree(folder, onexc=lambda _function, path, exc: logger.warning(
            f"Could not remove the dropped file {path}: {exc}"))


def receive(pasteboard, support_dir: Path, on_files: Callable[[list[str]], None],
            on_failure: Callable[[str], None]) -> bool:
    """Hand the dropped media to `on_files` as paths: at once for files on
    disk; for promised ones later, on the main thread, once the app they come
    from has written them under received_folder(). `on_failure` is told why
    when a promised file did not arrive. False when the drag carries no media."""
    files = _files(pasteboard)
    if files:
        on_files(files)
        return True
    promises = _promises(pasteboard)
    if not promises:
        return False
    try:
        received_folder(support_dir).mkdir(parents=True, exist_ok=True)
        # A folder per drop: two recordings of one name do not meet.
        folder = tempfile.mkdtemp(dir=received_folder(support_dir))
    except OSError as exc:
        logger.error(f"Could not make a folder for dropped files in {received_folder(support_dir)}: {exc}")
        on_failure("Maramax could not make room for it on disk")
        return True
    awaited = sum(len(receiver.fileTypes()) for receiver in promises)
    arrived: list[str] = []
    problems: list[str] = []
    settled = False

    def settle() -> None:
        """Once: when every promised file has arrived or failed, or the wait is over."""
        nonlocal settled
        if settled:
            return
        settled = True
        promises.clear()  # Held until here: nothing else is known to keep the receivers alive.
        if arrived:
            on_files(list(arrived))
        else:
            shutil.rmtree(folder, ignore_errors=True)  # Empty, or about to be: discard_received() takes the rest.
        if problems:
            on_failure(problems[0])
        elif len(arrived) < awaited:
            on_failure("the app it came from did not hand it over")

    def reader(url, error) -> None:
        if settled:
            logger.warning(f"A dropped file arrived after Maramax stopped waiting for it: {url}")
            return
        if error is None:
            arrived.append(str(url.path()))
        else:
            problems.append(str(error.localizedDescription()))
            logger.error(f"A dropped file was promised but not written into {folder}: {error}")
        if len(arrived) + len(problems) >= awaited:
            settle()

    for receiver in list(promises):
        receiver.receivePromisedFilesAtDestination_options_operationQueue_reader_(
            NSURL.fileURLWithPath_isDirectory_(folder, True), {}, NSOperationQueue.mainQueue(), reader)
    call_later(PROMISE_SECONDS, settle)
    return True
