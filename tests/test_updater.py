"""Finding, verifying, and swapping in a new release, with file URLs and a fake bundle. No network, no app is opened."""
import hashlib
import http.client
import io
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import zipfile
from types import SimpleNamespace

import pytest

from parakeet_dictation import bundle_delta, update_offer, update_prompt, update_window, updater
from parakeet_dictation.config import AppConfig


@pytest.fixture(autouse=True)
def no_real_dialogs(monkeypatch):
    """A test that reaches a real alert or window would put it on the screen and wait for a click."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to show a real dialog")
    monkeypatch.setattr(update_offer.rumps, "alert", refuse)
    monkeypatch.setattr(update_offer, "UpdatePromptWindow", SimpleNamespace(alloc=refuse))


def release_payload(tag="v0.5.2", assets=("Maramax-0.5.2.zip", "Maramax-0.5.2.zip.sha256"), body="Fixes."):
    return {
        "tag_name": tag, "body": body, "html_url": f"https://github.com/x/maramax/releases/tag/{tag}",
        "assets": [{"name": name, "size": 2048, "state": "uploaded",
                    "browser_download_url": f"https://example.test/{name}"} for name in assets],
    }


@pytest.mark.parametrize("tag, expected", [("v0.5.2", "0.5.2"), ("0.6", "0.6"), ("v0.10.0", "0.10.0")])
def test_a_newer_tag_is_an_update(tag, expected):
    release = updater.newer_release("0.5.1", release_payload(tag=tag))
    assert release is not None and release.version == expected
    assert release.archive.url.endswith(".zip") and release.archive.checksum_url.endswith(".zip.sha256")
    assert release.notes == "Fixes." and release.archive.size == 2048 and release.delta is None


def test_a_delta_from_the_installed_version_is_picked_up_and_older_clients_see_one_zip():
    names = ("Maramax-0.5.2.zip", "Maramax-0.5.2.zip.sha256",
             "Maramax-0.5.2-from-0.5.1.delta", "Maramax-0.5.2-from-0.5.1.delta.sha256",
             "Maramax-0.5.2-from-0.5.0.delta", "Maramax-0.5.2-from-0.5.0.delta.sha256")
    release = updater.newer_release("0.5.1", release_payload(assets=names))
    assert release.delta is not None and release.delta.name == "Maramax-0.5.2-from-0.5.1.delta"
    assert updater.newer_release("0.4.0", release_payload(assets=names)).delta is None
    # What 0.6.1's updater requires of a release: exactly one asset ending in .zip.
    assert [name for name in names if name.endswith(".zip")] == ["Maramax-0.5.2.zip"]


@pytest.mark.parametrize("tag", ["v0.5.1", "v0.5.0", "0.4.9", "v0.5", "v0.5.1.0"])
def test_the_same_or_an_older_tag_is_not(tag):
    assert updater.newer_release("0.5.1", release_payload(tag=tag)) is None


def test_versions_compare_as_numbers_and_ignore_trailing_zeros():
    assert updater.version_key("0.10.0") > updater.version_key("0.9.9")
    assert updater.version_key("v0.6") == updater.version_key("0.6.0")


def still_uploading():
    payload = release_payload()
    payload["assets"][0]["state"] = "starter"
    return payload


def without_url():
    payload = release_payload()
    del payload["assets"][1]["browser_download_url"]
    return payload


@pytest.mark.parametrize("payload, message", [
    (release_payload(tag="nightly"), "not a version number"),
    (release_payload(assets=("Maramax-0.5.2.zip",)), "without Maramax-0.5.2.zip.sha256"),
    (release_payload(assets=("a.zip", "a.zip.sha256", "b.zip", "b.zip.sha256")), "one app .zip"),
    ({"assets": []}, "no tag"),
    (still_uploading(), "still being published"),
    (without_url(), "still being published"),
])
def test_a_malformed_release_is_reported_not_ignored(payload, message):
    with pytest.raises(updater.UpdateError, match=message):
        updater.newer_release("0.5.1", payload)


@pytest.mark.parametrize("code, message", [(404, "no published release"), (403, "limiting"), (500, "HTTP 500")])
def test_an_http_failure_says_what_happened(monkeypatch, code, message):
    def fail(url, version):
        raise urllib.error.HTTPError(url, code, "No", {}, io.BytesIO())
    monkeypatch.setattr(updater, "_open", fail)
    with pytest.raises(updater.UpdateError, match=message):
        updater.latest_release("0.5.1")


def test_an_unreachable_server_is_an_error_that_says_so(monkeypatch):
    def offline(url, version):
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(updater, "_open", offline)
    with pytest.raises(updater.UpdateError, match="Could not reach GitHub"):
        updater.latest_release("0.5.1")


def test_the_latest_release_is_read_from_the_api(tmp_path):
    answer = tmp_path / "latest.json"
    answer.write_text(json.dumps(release_payload()))
    release = updater.latest_release("0.5.1", url=answer.as_uri())
    assert release is not None and release.version == "0.5.2"


def test_checksum_files_are_parsed_strictly():
    digest = "a" * 64
    assert updater.parse_checksum(f"{digest}  Maramax-0.5.2.zip\n") == digest
    for text in ("not a digest", ""):
        with pytest.raises(updater.UpdateError):
            updater.parse_checksum(text)


def test_only_the_pinned_certificate_is_accepted():
    requirement = updater.signer_requirement("com.maramax.dictation")
    assert requirement == ('identifier "com.maramax.dictation" and '
                           f'certificate leaf = H"{updater.SIGNER_CERTIFICATE_SHA1}"')
    assert len(updater.SIGNER_CERTIFICATE_SHA1) == 40


# -- Download --

def fake_bundle(root, identifier="com.maramax.dictation", version="0.5.2"):
    app = root / "Maramax.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": identifier, "CFBundleShortVersionString": version}))
    (app / "Contents" / "MacOS" / "Maramax").write_text("#!/bin/sh\n")
    return app


def version_of(app):
    return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"]


def published(tmp_path, identifier="com.maramax.dictation", version="0.5.2", tamper=False, modes=None,
              info_plist=None):
    """A release archive laid out as create_release.py makes it, served from files.
    `modes` sets permissions inside the app; `info_plist` replaces its Info.plist."""
    source = tmp_path / "source"
    app = fake_bundle(source / f"Maramax-{version}", identifier, version)
    for relative, mode in (modes or {}).items():
        (app / relative).chmod(mode)
    if info_plist is not None:
        (app / "Contents" / "Info.plist").write_bytes(info_plist)
    archive = tmp_path / f"Maramax-{version}.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in sorted(source.rglob("*")):
            zipped.write(path, path.relative_to(source))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if tamper:
        archive.write_bytes(archive.read_bytes() + b"x")
    checksum = tmp_path / f"{archive.name}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n")
    return updater.Release(version=version, notes="", page_url="", delta=None, archive=updater.Asset(
        archive.name, archive.as_uri(), archive.stat().st_size, checksum.as_uri()))


@pytest.fixture
def codesign(monkeypatch):
    """codesign as a recorder: the fake bundle has no signature to verify."""
    real = updater._run
    seen = []

    def run(command, what):
        if command[0] == "codesign":
            seen.append(command)
        else:
            real(command, what)
    monkeypatch.setattr(updater, "_run", run)
    return seen


def test_a_verified_download_is_placed_beside_the_installed_app(tmp_path, codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    progress = []
    staged = updater.download(published(tmp_path), "0.5.1", installed, tmp_path / "staging",
                              lambda received, expected: progress.append(received / expected), lambda: False)
    assert staged == installed.parent / updater.STAGED_NAME and version_of(staged) == "0.5.2"
    assert progress[-1] == 1.0
    assert codesign == [["codesign", "--verify", "--deep", "--strict",
                         f"-R={updater.signer_requirement('com.maramax.dictation')}",
                         str(tmp_path / "staging" / "unpacked" / "Maramax-0.5.2" / "Maramax.app")]]


@pytest.mark.parametrize("kwargs, message", [
    ({"tamper": True}, "does not match its published SHA-256"),
    ({"identifier": "com.example.other"}, "com.example.other"),
])
def test_a_download_that_is_not_this_app_is_refused_and_cleared(tmp_path, codesign, kwargs, message):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    with pytest.raises(updater.UpdateError, match=message):
        updater.download(published(tmp_path, **kwargs), "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: False)
    assert codesign == []
    assert not (tmp_path / "staging").exists()
    assert not (installed.parent / updater.STAGED_NAME).exists()


def test_a_download_cut_short_says_so_rather_than_blaming_the_checksum(tmp_path, codesign):
    """A connection closed cleanly mid-body ends the reads with no error."""
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    release = published(tmp_path)
    longer = updater.Asset(**{**release.archive.__dict__, "size": release.archive.size + 4096})
    cut_short = updater.Release(**{**release.__dict__, "archive": longer})
    with pytest.raises(updater.UpdateError, match=f"stopped after {release.archive.size:,} of {longer.size:,} bytes"):
        updater.download(cut_short, "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: False)
    assert not (tmp_path / "staging").exists() and codesign == []


@pytest.mark.parametrize("failure", [
    http.client.IncompleteRead(b"partial", 2048),      # a chunked body cut off: not an OSError
    ConnectionResetError(54, "Connection reset by peer"),
])
def test_a_connection_that_breaks_mid_download_is_a_download_error_and_is_cleared(tmp_path, monkeypatch, failure):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    release = published(tmp_path)
    real_open = updater._open

    class Breaking:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, amount):
            raise failure
    monkeypatch.setattr(updater, "_open", lambda url, version: Breaking() if url == release.archive.url
                        else real_open(url, version))
    with pytest.raises(updater.UpdateError, match="Could not download Maramax-0.5.2.zip"):
        updater.download(release, "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: False)
    assert not (tmp_path / "staging").exists()


def test_a_download_whose_app_reports_another_version_is_refused(tmp_path, codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    release = published(tmp_path, version="0.5.2")
    mislabelled = updater.Release(**{**release.__dict__, "version": "0.5.3"})
    with pytest.raises(updater.UpdateError, match="says it is version 0.5.2"):
        updater.download(mislabelled, "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: False)


def test_a_signature_from_another_certificate_is_refused(tmp_path, monkeypatch):
    real = updater._run

    def unsigned(command, what):
        if command[0] == "codesign":
            raise updater.UpdateError(f"Could not {what}: test-requirement: code failed to satisfy")
        real(command, what)
    monkeypatch.setattr(updater, "_run", unsigned)
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    with pytest.raises(updater.UpdateError, match="release certificate"):
        updater.download(published(tmp_path), "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: False)
    assert not (installed.parent / updater.STAGED_NAME).exists()


def test_the_kept_previous_copy_and_read_only_folders_are_refused_before_downloading(tmp_path):
    updates = tmp_path / "updates"
    kept = fake_bundle(updates / "previous", version="0.5.0")
    with pytest.raises(updater.UpdateError, match="kept from the last update"):
        updater.ensure_installable(kept, updates)
    locked = tmp_path / "Locked"
    app = fake_bundle(locked)
    os.chmod(locked, 0o555)
    try:
        with pytest.raises(updater.UpdateError, match="not writable"):
            updater.ensure_installable(app, updates)
    finally:
        os.chmod(locked, 0o755)
    # A folder that is not writable itself cannot be renamed aside by the swap,
    # even in a writable Applications folder (another user's copy, or root's).
    os.chmod(app, 0o555)
    try:
        with pytest.raises(updater.UpdateError, match="another user or is read-only"):
            updater.ensure_installable(app, updates)
    finally:
        os.chmod(app, 0o755)
    updater.ensure_installable(fake_bundle(tmp_path / "Applications"), updates)


def test_a_copy_left_hidden_by_a_failed_swap_is_not_updated_in_place(tmp_path):
    """Running from .Maramax-replaced.app, the swap would move that folder
    onto itself and delete it, leaving no Maramax at all."""
    (tmp_path / "Applications").mkdir()
    hidden = fake_bundle(tmp_path / "old", version="0.5.1").rename(tmp_path / "Applications" / updater.REPLACED_NAME)
    with pytest.raises(updater.UpdateError, match="Rename it to Maramax.app"):
        updater.ensure_installable(hidden, tmp_path / "updates")
    staged = fake_bundle(tmp_path / "new").rename(tmp_path / "Applications" / updater.STAGED_NAME)
    script = tmp_path / "install.sh"
    (tmp_path / "updates").mkdir()
    exited = subprocess.Popen(["true"])
    exited.wait()
    script.write_text(updater.swap_script(
        pid=exited.pid, staged_app=staged, installed_app=hidden, previous_app=tmp_path / "updates" / "previous" / "M.app",
        staging=tmp_path / "updates" / "download", result_path=tmp_path / "updates" / "last-install",
        log_path=tmp_path / "update.log"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "open").write_text("#!/bin/sh\n")
    (fake_bin / "open").chmod(0o755)
    subprocess.run(["/bin/sh", str(script)], env={"PATH": f"{fake_bin}:/usr/bin:/bin"}, check=False, timeout=30)
    assert version_of(hidden) == "0.5.1"      # The script refuses too: nothing was deleted.
    assert updater.take_install_result(tmp_path / "updates" / "last-install") is updater.InstallResult.NOT_MOVED_ASIDE


# -- The swap after quitting --

def run_swap(tmp_path, *, staged, pid=None, failing_mv_sources=()):
    """Run the real script with `open` (and optionally `mv`) replaced."""
    installed = tmp_path / "Applications" / "Maramax.app"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    opened = tmp_path / "opened.txt"
    (fake_bin / "open").write_text(f'#!/bin/sh\necho "$1" >> "{opened}"\n')
    (fake_bin / "open").chmod(0o755)
    if failing_mv_sources:
        refusals = "".join(f'[ "$1" = "{source}" ] && exit 1\n' for source in failing_mv_sources)
        (fake_bin / "mv").write_text(f'#!/bin/sh\n{refusals}exec /bin/mv "$@"\n')
        (fake_bin / "mv").chmod(0o755)
    if pid is None:
        exited = subprocess.Popen(["true"])
        exited.wait()
        pid = exited.pid
    updates = tmp_path / "updates"
    updates.mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    script = tmp_path / "install.sh"
    script.write_text(updater.swap_script(
        pid=pid, staged_app=staged, installed_app=installed, previous_app=updates / "previous" / "Maramax.app",
        staging=updates / "download", result_path=updates / "last-install", log_path=tmp_path / "logs" / "update.log"))
    subprocess.run(["/bin/sh", str(script)], env={"PATH": f"{fake_bin}:/usr/bin:/bin"}, check=False, timeout=30)
    result = updater.take_install_result(updates / "last-install")
    return installed, result, opened.read_text().splitlines() if opened.exists() else []


def staged_beside(tmp_path):
    fake_bundle(tmp_path / "Applications", version="0.5.1")
    staged = fake_bundle(tmp_path / "unpacked").rename(tmp_path / "Applications" / updater.STAGED_NAME)
    (tmp_path / "updates" / "download").mkdir(parents=True)
    return staged


def test_the_swap_installs_the_new_app_and_keeps_the_old_one(tmp_path):
    installed, result, opened = run_swap(tmp_path, staged=staged_beside(tmp_path))
    assert result is updater.InstallResult.INSTALLED and version_of(installed) == "0.5.2"
    assert version_of(tmp_path / "updates" / "previous" / "Maramax.app") == "0.5.1"
    assert not (tmp_path / "updates" / "download").exists()
    assert not (installed.parent / updater.STAGED_NAME).exists()
    assert not (installed.parent / updater.REPLACED_NAME).exists()
    assert opened == [str(installed)]


def test_a_swap_that_cannot_place_the_new_app_restores_the_old_one(tmp_path):
    staged = staged_beside(tmp_path)
    installed, result, opened = run_swap(tmp_path, staged=staged, failing_mv_sources=[staged])
    assert result is updater.InstallResult.NOT_PLACED and version_of(installed) == "0.5.1"
    assert opened == [str(installed)]


def test_a_swap_that_cannot_put_either_app_in_place_opens_the_old_one_and_says_so(tmp_path):
    staged = staged_beside(tmp_path)
    replaced = tmp_path / "Applications" / updater.REPLACED_NAME
    installed, result, opened = run_swap(tmp_path, staged=staged, failing_mv_sources=[staged, replaced])
    assert result is updater.InstallResult.NOT_RESTORED and not installed.exists()
    assert version_of(replaced) == "0.5.1" and opened == [str(replaced)]
    assert ".Maramax-replaced.app" in update_offer.install_failure(result)


def test_a_swap_that_cannot_move_the_old_app_aside_changes_nothing(tmp_path):
    staged = staged_beside(tmp_path)
    installed = tmp_path / "Applications" / "Maramax.app"
    _, result, opened = run_swap(tmp_path, staged=staged, failing_mv_sources=[installed])
    assert result is updater.InstallResult.NOT_MOVED_ASIDE and version_of(installed) == "0.5.1"
    assert not staged.exists() and opened == [str(installed)]


def test_a_missing_new_app_leaves_the_old_one_running(tmp_path):
    fake_bundle(tmp_path / "Applications", version="0.5.1")
    installed, result, opened = run_swap(tmp_path, staged=tmp_path / "Applications" / updater.STAGED_NAME)
    assert result is updater.InstallResult.STAGED_MISSING and version_of(installed) == "0.5.1"
    assert opened == [str(installed)]


def test_an_app_that_never_quits_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "QUIT_WAIT_SECONDS", 0.3)
    staged = staged_beside(tmp_path)
    alive = subprocess.Popen(["sleep", "30"])
    try:
        started = time.monotonic()
        installed, result, opened = run_swap(tmp_path, staged=staged, pid=alive.pid)
    finally:
        alive.kill()
    assert result is updater.InstallResult.NOT_QUIT and version_of(installed) == "0.5.1"
    assert time.monotonic() - started < 10 and opened == [] and not staged.exists()


def test_paths_with_spaces_and_quotes_survive_the_script(tmp_path):
    root = tmp_path / "Application Support" / "it's here"
    root.mkdir(parents=True)
    installed, result, _ = run_swap(root, staged=staged_beside(root))
    assert result is updater.InstallResult.INSTALLED and version_of(installed) == "0.5.2"


def test_an_unknown_result_is_reported_and_read_once(tmp_path):
    path = tmp_path / "last-install"
    assert updater.take_install_result(path) is None
    path.write_text("installed\n")
    assert updater.take_install_result(path) is updater.InstallResult.INSTALLED
    assert updater.take_install_result(path) is None
    path.write_text("exploded")
    with pytest.raises(updater.UpdateError, match="unknown result"):
        updater.take_install_result(path)


# -- The menu's offer --

@pytest.mark.parametrize("asked, skipped, busy, expected", [
    (True, "0.5.2", True, True),      # Asked for: always answered.
    (False, None, False, True),
    (False, "0.5.2", False, False),   # Skipped by the user.
    (False, None, True, False),       # Never interrupts a dictation.
])
def test_when_a_found_update_is_offered(asked, skipped, busy, expected):
    assert update_offer.should_prompt(asked=asked, version="0.5.2", skipped_version=skipped, busy=busy) is expected


@pytest.mark.parametrize("idle_now, idle_before, expected", [
    (True, True, True), (True, False, False), (False, True, False), (False, False, False),
])
def test_an_install_needs_two_idle_looks(idle_now, idle_before, expected):
    assert update_offer.ready_to_install(idle_now=idle_now, idle_before=idle_before) is expected


def test_the_menu_item_says_what_is_happening_in_a_few_words():
    """The menu is a fixed width: a long title would be cut off."""
    title, step = update_offer.menu_title, update_offer.Step
    assert title(step.IDLE, None) == update_offer.CHECK_TITLE
    assert title(step.IDLE, "0.5.2") == "Install Maramax 0.5.2…"
    assert title(step.CHECKING, "0.5.2") == "Checking for Updates…"
    assert title(step.DOWNLOADING, "0.5.2", 40) == "Downloading Update… 40%"
    assert title(step.CANCELLING, "0.5.2") == "Cancelling the Update…"
    assert title(step.INSTALLING, "0.5.2") == "Installing Maramax 0.5.2…"


FAILED = update_offer.CheckFailed("Could not reach GitHub")


@pytest.mark.parametrize("step, version, last_check, updated_to, expected", [
    (update_offer.Step.IDLE, None, "not checked", None, "Not checked yet."),
    (update_offer.Step.IDLE, None, "succeeded", None, "This is the newest version."),
    (update_offer.Step.IDLE, None, FAILED, None, "The last check did not work: Could not reach GitHub"),
    (update_offer.Step.IDLE, "0.6.1", "succeeded", None, "Maramax 0.6.1 is available."),
    (update_offer.Step.IDLE, None, "succeeded", "0.6.1", "Updated to Maramax 0.6.1."),
    (update_offer.Step.CHECKING, None, "succeeded", None, "Checking for updates…"),
    (update_offer.Step.CANCELLING, "0.6.1", "succeeded", None, "Cancelling the update to Maramax 0.6.1…"),
    (update_offer.Step.INSTALLING, "0.6.1", "succeeded", None,
     "Maramax 0.6.1 is ready and installs as soon as Maramax is idle."),
])
def test_settings_says_where_updates_stand(step, version, last_check, updated_to, expected):
    assert update_offer.status_text(step=step, version=version, percent=None, last_check=last_check,
                                    updated_to=updated_to) == expected


@pytest.mark.parametrize("size, expected", [
    (2048, "2 KB"), (100, "1 KB"), (840 * 1024, "840 KB"), (210_252_034, "201 MB"), (1024 * 1024, "1.0 MB"),
    (3_565_158, "3.4 MB"),
])
def test_download_sizes_read_naturally(size, expected):
    assert update_window.download_size(size) == expected
    assert update_window.progress_state(size // 2, size) == (
        f"{update_window.download_size(size // 2)} of {expected}", (size // 2) / size)
    assert update_window.progress_state(size, size) == ("Checking the download…", 1.0)


def test_every_failed_install_has_something_to_say():
    for result in updater.InstallResult:
        message = update_offer.install_failure(result)
        assert (message is None) is (result is updater.InstallResult.INSTALLED)


def test_release_notes_are_read_as_headings_bullets_and_paragraphs():
    from parakeet_dictation.update_prompt import Bullet, Emphasis, Heading, Link, Paragraph, Run, note_blocks

    notes = ("## New\r\n\n- **Updates itself.** See [the guide](https://x.test)\n  and `START HERE.md`.\n"
             "* _quietly_ fixed\n\nUpdating from 0.6.3 downloads\nabout 4 MB.\n\n[odd](javascript:alert(1)) snake_case")
    assert note_blocks(notes) == [
        Heading((Run("New", Emphasis.PLAIN),)),
        Bullet((Run("Updates itself.", Emphasis.STRONG), Run(" See ", Emphasis.PLAIN),
                Link("the guide", "https://x.test"), Run(" and ", Emphasis.PLAIN),
                Run("START HERE.md", Emphasis.CODE), Run(".", Emphasis.PLAIN))),
        Bullet((Run("quietly", Emphasis.ITALIC), Run(" fixed", Emphasis.PLAIN))),
        Paragraph((Run("Updating from 0.6.3 downloads about 4 MB.", Emphasis.PLAIN),)),
        # Only web links are links: the notes are not covered by the release's signature.
        Paragraph((Run("[odd](javascript:alert(1)) snake_case", Emphasis.PLAIN),)),
    ]
    assert note_blocks("") == [] and note_blocks("\n  \n") == []


def test_a_numbered_list_keeps_its_items_and_their_numbers():
    from parakeet_dictation.update_prompt import Emphasis, Heading, NumberedItem, Paragraph, Run, note_blocks

    def plain(text):
        return (Run(text, Emphasis.PLAIN),)
    assert note_blocks("## Fixes\n1. First fix\n2. Second fix\n   that wraps\n10) Tenth\n\nAbout 1.5 MB.") == [
        Heading(plain("Fixes")),
        NumberedItem("1.", plain("First fix")),
        NumberedItem("2.", plain("Second fix that wraps")),
        NumberedItem("10)", plain("Tenth")),
        Paragraph(plain("About 1.5 MB.")),     # A number in a sentence starts no list.
    ]


def test_the_offer_says_what_installing_does():
    assert update_prompt.offer_text("0.8.0", "0.7.0") == (
        "Maramax 0.8.0 is now available—you have 0.7.0. Would you like to install it now?")
    assert "about 4.0 MB" in update_prompt.install_note("4.0 MB")


class FakeWindow:
    shown: list = []

    @classmethod
    def alloc(cls):
        return cls()

    def initWithCancel_(self, on_cancel):
        self.on_cancel = on_cancel
        return self

    def __getattr__(self, name):
        return lambda *args, **kwargs: FakeWindow.shown.append((name, args))


class FakePrompt:
    """The Software Update window: records what it was asked to show."""
    shown: list = []    # (call, keyword arguments)
    offered: list = []  # the versions it showed

    @classmethod
    def alloc(cls):
        return cls()

    def initWithChoice_(self, on_choice):
        self.on_choice = on_choice
        return self

    def show(self, **kwargs):
        FakePrompt.shown.append(("show", kwargs))
        FakePrompt.offered.append(kwargs["version"])

    def withdraw(self):
        FakePrompt.shown.append(("withdraw", {}))


def offer(monkeypatch, tmp_path, busy=False, modal=None):
    FakeWindow.shown = []
    FakePrompt.shown = []
    FakePrompt.offered = []
    monkeypatch.setattr(update_offer, "UpdateProgressWindow", FakeWindow)
    monkeypatch.setattr(update_offer, "UpdatePromptWindow", FakePrompt)
    alerts = []
    monkeypatch.setattr(update_offer.rumps, "alert", lambda **kwargs: alerts.append(kwargs) or 1)
    monkeypatch.setattr(update_offer, "NSApplication",
                        SimpleNamespace(sharedApplication=lambda: SimpleNamespace(modalWindow=lambda: modal)))
    item = SimpleNamespace(title=update_offer.CHECK_TITLE)
    config = AppConfig()
    saved = []
    controller = update_offer.UpdateOffer(
        menu_item=item, current_version="0.5.1", installed_app=None, support_dir=tmp_path, config=config,
        save_settings=lambda: saved.append(True) or True, is_busy=lambda: busy, quit_app=lambda: None,
        on_change=lambda: None)
    return controller, item, config, alerts, saved, FakePrompt.offered


def a_release():
    return updater.newer_release("0.5.1", release_payload())


def test_skipping_a_version_is_remembered(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    release = a_release()
    controller._checked(release, asked=False)
    assert asked == ["0.5.2"]
    controller._answered(update_prompt.Choice.SKIP)
    assert config.skipped_update_version == "0.5.2" and saved
    assert item.title == "Install Maramax 0.5.2…"
    controller._checked(release, asked=False)   # The next daily check stays quiet.
    assert asked == ["0.5.2"]


def test_an_offer_takes_the_keyboard_only_when_the_user_asked(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    controller._checked(a_release(), asked=False)
    name, shown = FakePrompt.shown[-1]
    assert name == "show" and shown["activate"] is False and shown["notes"] == "Fixes."
    assert shown["current_version"] == "0.5.1" and shown["size"] == "2 KB"
    controller._answered(update_prompt.Choice.LATER)
    assert controller.can_check() and config.skipped_update_version is None and not saved
    assert item.title == "Install Maramax 0.5.2…"      # Later keeps the offer in the menu.
    controller._checked(a_release(), asked=True)
    assert FakePrompt.shown[-1][1]["activate"] is True


def test_an_unanswered_offer_is_taken_back_by_the_next_check(monkeypatch, tmp_path):
    """A window left behind another app must not stop the daily check, nor
    install, days later, a release GitHub has since replaced."""
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(update_offer.threading, "Thread",
                        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["args"])))
    monkeypatch.setattr(update_offer, "call_later", lambda *args: None)
    controller._checked(a_release(), asked=False)
    assert controller.can_check()                     # Settings' Check Now still works.
    controller._scheduled_check()
    assert FakePrompt.shown[-1][0] == "withdraw" and started == [(False,)]
    assert item.title == "Checking for Updates…"
    newer = updater.newer_release("0.5.1", release_payload(tag="v0.5.3", assets=(
        "Maramax-0.5.3.zip", "Maramax-0.5.3.zip.sha256")))
    controller._checked(newer, asked=False)
    assert asked == ["0.5.2", "0.5.3"]
    controller.check_requested()                       # The menu item, too, asks again.
    assert FakePrompt.shown[-1][0] == "withdraw" and started[-1] == (True,)


def test_install_update_starts_the_download(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    downloads = []
    controller._download = downloads.append
    controller._checked(a_release(), asked=False)
    controller._answered(update_prompt.Choice.INSTALL)
    assert [release.version for release in downloads] == ["0.5.2"] and controller.can_check()


def test_an_automatic_check_never_alerts_about_a_failure_or_no_update(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    controller._check_failed("Could not reach GitHub", asked=False)
    controller._checked(None, asked=False)
    assert alerts == [] and asked == [] and item.title == update_offer.CHECK_TITLE


def test_a_requested_check_reports_that_the_app_is_current(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    controller._checked(None, asked=True)
    assert alerts[0] == {"title": "You’re up to date!",
                         "message": "Maramax 0.5.1 is currently the newest version available."}


def test_a_failed_check_is_shown_until_one_succeeds(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    changes = []
    controller._on_change = lambda: changes.append(controller.status_text())
    controller._check_failed("Could not reach GitHub", asked=False)
    assert changes[-1] == "The last check did not work: Could not reach GitHub"
    controller._checked(None, asked=False)
    assert changes[-1] == "This is the newest version." and controller.can_check()


def run_timers(monkeypatch):
    """call_later as a list the test steps through."""
    later = []
    monkeypatch.setattr(update_offer, "call_later", lambda delay, function, *args: later.append((delay, function, args)))

    def tick():
        delay, function, args = later.pop(0)
        function(*args)
        return delay
    return later, tick


def test_install_and_relaunch_shows_progress_waits_for_idle_then_restarts(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    busy = [True]
    controller._is_busy = lambda: busy[0]
    later, tick = run_timers(monkeypatch)
    installs, quits = [], []
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    monkeypatch.setattr(update_offer.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    controller._quit_app = lambda: quits.append(True)
    controller._installed_app = tmp_path / "Applications" / "Maramax.app"
    controller._installed_app.mkdir(parents=True)
    release = a_release()
    controller._download(release)
    controller._show_download_progress(1024, 2048)
    assert item.title == "Downloading Update… 50%"
    controller._staged(release, controller._installed_app, tmp_path / updater.STAGED_NAME)
    assert item.title == "Installing Maramax 0.5.2…"
    tick()                      # Still dictating.
    busy[0] = False
    tick()                      # Idle once: a finished dictation may still be pasting.
    assert not installs
    tick()                      # Idle twice: "Restarting Maramax…" goes on screen first.
    assert not installs and later[0][0] == update_offer.RESTART_NOTICE_SECONDS
    tick()
    assert installs[0]["previous_app"] == tmp_path / "updates" / "previous" / "Maramax.app"
    assert installs[0]["result_path"] == tmp_path / "updates" / "last-install" and quits
    steps = [name for name, _ in FakeWindow.shown]
    assert steps[0] == "show" and "show_progress" in steps and "show_ready" in steps
    assert steps[-1] == "show_restarting"


def test_a_dictation_started_during_the_restart_notice_holds_the_install(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    later, tick = run_timers(monkeypatch)
    installs = []
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    release = a_release()
    controller._release = release
    controller._set_step(update_offer.Step.INSTALLING)
    controller._install_when_idle(release, tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME, idle_before=True)
    controller._is_busy = lambda: True          # Option+Space during "Restarting Maramax…"
    tick()
    assert not installs and later              # Back to waiting, not quitting.


def test_an_open_dialog_holds_the_install(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path, modal=object())
    later, tick = run_timers(monkeypatch)
    installs = []
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    controller._install_when_idle(a_release(), tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME,
                                  idle_before=True)
    assert not installs and later


def test_a_quit_that_does_not_happen_is_reported_and_reset(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    later, tick = run_timers(monkeypatch)
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: None)
    release = a_release()
    controller._release = release
    controller._set_step(update_offer.Step.INSTALLING)
    controller._restart(release, tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME)
    result = tmp_path / "updates" / "last-install"
    result.parent.mkdir()
    result.write_text(f"{updater.InstallResult.NOT_QUIT}\n")   # The swap script gave up first.
    assert tick() == update_offer.QUIT_WATCHDOG_SECONDS
    assert alerts[0]["title"] == "The update was not installed" and controller.can_check()
    # Already said: the next launch does not report the same attempt again.
    assert not result.exists()
    later.clear()
    controller.start()
    assert not [delay for delay, function, args in later if delay == 5] and len(alerts) == 1


def test_a_failed_download_is_reported_and_the_offer_stays(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    release = a_release()
    controller._release = release
    controller._download_failed(release, "does not match its published SHA-256")
    assert "SHA-256" in alerts[0]["message"]
    assert item.title == "Install Maramax 0.5.2…"


def test_cancel_stops_the_download_quietly_and_discards_it(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    discarded = []
    monkeypatch.setattr(update_offer.UpdateOffer, "_discard", staticmethod(discarded.append))
    release = a_release()
    controller._release = release
    controller._window = FakeWindow()
    controller._set_step(update_offer.Step.DOWNLOADING, 10)
    controller.cancel_requested()
    controller._download_stopped(release)
    assert alerts == [] and item.title == "Install Maramax 0.5.2…"
    # A cancel that arrives after the download finished discards what it staged:
    controller._set_step(update_offer.Step.DOWNLOADING, 100)
    controller._staged(release, tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME)
    assert discarded == [tmp_path / updater.STAGED_NAME] and controller.can_check()


def test_the_next_launch_reports_how_the_last_update_went(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    later, tick = run_timers(monkeypatch)
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates" / "last-install").write_text("installed")
    controller.start()
    assert controller.status_text() == "Updated to Maramax 0.5.1."
    (tmp_path / "updates" / "last-install").write_text("not-placed")
    later.clear()
    controller.start()
    alert = next(function for delay, function, args in later if delay == 5)
    alert()
    assert alerts[0]["title"] == "The update was not installed" and "restored" in alerts[0]["message"]


# -- Deltas: only what changed --

def tree(root, version, *, files, links=(), dirs=()):
    app = fake_bundle(root, version=version)
    for relative, text in files.items():
        (app / relative).parent.mkdir(parents=True, exist_ok=True)
        (app / relative).write_text(text)
    for relative in dirs:
        (app / relative).mkdir(parents=True, exist_ok=True)
    for relative, target in links:
        (app / relative).parent.mkdir(parents=True, exist_ok=True)
        (app / relative).symlink_to(target)
    return app


def two_versions(tmp_path):
    old = tree(tmp_path / "old", "0.5.1",
               files={"Contents/Resources/lib/big.bin": "x" * 50_000, "Contents/Resources/gone.txt": "removed",
                      "Contents/Resources/pkg_removed/__init__.py": "", "Contents/Resources/was_file": "file",
                      "Contents/Frameworks/F/Versions/1/lib": "one", "Contents/Frameworks/F/Versions/2/lib": "two"},
               links=[("Contents/Frameworks/F/Versions/Current", "1")])
    new = tree(tmp_path / "new", "0.5.2",
               files={"Contents/Resources/lib/big.bin": "x" * 50_000, "Contents/Resources/added.txt": "new",
                      "Contents/Resources/was_file/now_a_dir.txt": "inside",
                      "Contents/Frameworks/F/Versions/1/lib": "one", "Contents/Frameworks/F/Versions/2/lib": "two"},
               links=[("Contents/Frameworks/F/Versions/Current", "2")])
    (new / "Contents" / "Resources" / "added.txt").chmod(0o600)
    return old, new


def apply_to_a_clone(tmp_path, old, delta):
    rebuilt = tmp_path / "rebuilt" / "Maramax.app"
    rebuilt.parent.mkdir()
    subprocess.run(["cp", "-cR", str(old), str(rebuilt)], check=True)
    bundle_delta.apply(delta, rebuilt)
    return rebuilt


def test_a_delta_carries_only_what_changed_and_rebuilds_the_new_tree_exactly(tmp_path):
    old, new = two_versions(tmp_path)
    delta = tmp_path / "delta"
    changed, deleted = bundle_delta.make(old, new, delta)
    assert deleted == 3             # gone.txt, and pkg_removed/ with its file
    assert not (delta / "files" / "Contents" / "Resources" / "lib" / "big.bin").exists()
    rebuilt = apply_to_a_clone(tmp_path, old, delta)
    assert bundle_delta.entries(rebuilt) == bundle_delta.entries(new)
    assert not (rebuilt / "Contents" / "Resources" / "pkg_removed").exists()   # No empty package left behind.
    assert os.readlink(rebuilt / "Contents" / "Frameworks" / "F" / "Versions" / "Current") == "2"
    assert (rebuilt / "Contents" / "Resources" / "added.txt").stat().st_mode & 0o777 == 0o600


def test_a_delta_applied_to_the_wrong_base_is_refused(tmp_path):
    old, new = two_versions(tmp_path)
    delta = tmp_path / "delta"
    bundle_delta.make(old, new, delta)
    (old / "Contents" / "Resources" / "lib" / "big.bin").write_text("modified locally")
    with pytest.raises(bundle_delta.DeltaError, match="differs from the new version"):
        apply_to_a_clone(tmp_path, old, delta)


@pytest.mark.parametrize("bad_path", ["../outside.txt", "/etc/hosts", "Contents/../../outside.txt"])
def test_a_delta_cannot_name_a_path_outside_the_app(tmp_path, bad_path):
    old, new = two_versions(tmp_path)
    delta = tmp_path / "delta"
    bundle_delta.make(old, new, delta)
    manifest = json.loads((delta / "manifest.json").read_text())
    manifest["deleted"].append(bad_path)
    (delta / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(bundle_delta.DeltaError, match="outside the app"):
        apply_to_a_clone(tmp_path, old, delta)


def test_a_delta_cannot_write_through_a_symlink_it_creates(tmp_path):
    old, new = two_versions(tmp_path)
    delta = tmp_path / "delta"
    bundle_delta.make(old, new, delta)
    outside = tmp_path / "outside"
    outside.mkdir()
    (delta / "files" / "Contents" / "escape").symlink_to(outside)
    (delta / "files" / "Contents" / "escape-file").write_text("evil")
    manifest = json.loads((delta / "manifest.json").read_text())
    manifest["carried"] += ["Contents/escape", "Contents/escape/evil.txt"]
    manifest["tree"]["Contents/escape"] = ["link", str(outside), 0]
    manifest["tree"]["Contents/escape/evil.txt"] = ["file", "0" * 64, 0o644]
    (delta / "files" / "Contents" / "escape-file").rename(tmp_path / "unused")
    (delta / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(bundle_delta.DeltaError, match="through a symlink"):
        apply_to_a_clone(tmp_path, old, delta)
    assert not list(outside.iterdir())


def test_a_delta_cannot_pass_off_a_symlink_as_a_file(tmp_path):
    old, new = two_versions(tmp_path)
    delta = tmp_path / "delta"
    bundle_delta.make(old, new, delta)
    carried = delta / "files" / "Contents" / "Resources" / "added.txt"
    carried.unlink()
    carried.symlink_to(tmp_path / "secret")       # Would copy a file from outside the app into it.
    (tmp_path / "secret").write_text("private")
    with pytest.raises(bundle_delta.DeltaError, match="something other than a file"):
        apply_to_a_clone(tmp_path, old, delta)


def test_a_cancel_reaching_the_restart_installs_nothing(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    later, tick = run_timers(monkeypatch)
    installs, discarded = [], []
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    monkeypatch.setattr(update_offer.UpdateOffer, "_discard", staticmethod(discarded.append))
    release = a_release()
    controller._release = release
    controller._set_step(update_offer.Step.INSTALLING)
    controller._cancel.set()
    controller._restart(release, tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME)
    assert not installs and discarded and controller.can_check()


def test_an_installer_that_cannot_start_discards_the_new_app(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    discarded, quits = [], []
    monkeypatch.setattr(update_offer.UpdateOffer, "_discard", staticmethod(discarded.append))

    def cannot_start(**kwargs):
        raise updater.UpdateError("Could not start the installer: [Errno 28] No space left on device")
    monkeypatch.setattr(update_offer.updater, "install_after_exit", cannot_start)
    controller._quit_app = lambda: quits.append(True)
    release = a_release()
    controller._release = release
    controller._set_step(update_offer.Step.INSTALLING)
    controller._restart(release, tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME)
    assert discarded == [tmp_path / updater.STAGED_NAME] and not quits
    assert alerts[0]["title"] == "The update could not be installed" and controller.can_check()


def test_cancelling_the_wait_to_install_shows_at_once(monkeypatch, tmp_path):
    """The menu and Settings stop saying "Installing" the moment Cancel is
    pressed, though the staged app is discarded only at the next look."""
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path, busy=True)
    later, tick = run_timers(monkeypatch)
    installs, discarded = [], []
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    monkeypatch.setattr(update_offer.UpdateOffer, "_discard", staticmethod(discarded.append))
    controller._window = FakeWindow()
    release = a_release()
    controller._release = release
    controller._staged(release, tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME)
    assert item.title == "Installing Maramax 0.5.2…"
    controller.cancel_requested()
    assert item.title == "Cancelling the Update…" and not controller.can_check()
    assert controller.status_text() == "Cancelling the update to Maramax 0.5.2…"
    tick()
    assert discarded == [tmp_path / updater.STAGED_NAME] and not installs
    assert item.title == "Install Maramax 0.5.2…" and controller.can_check()


def test_the_strict_signature_check_catches_a_wrong_rebuild(tmp_path):
    """Real codesign, ad hoc, no keychain: what the updater relies on."""
    app = fake_bundle(tmp_path / "Applications")
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
    verify = ["codesign", "--verify", "--deep", "--strict", str(app)]
    assert subprocess.run(verify, capture_output=True).returncode == 0
    (app / "Contents" / "MacOS" / "extra").write_text("not in the seal")
    assert subprocess.run(verify, capture_output=True).returncode != 0


def published_with_delta(tmp_path, old_app, new_app, corrupt=False):
    release = published(tmp_path, version="0.5.2")
    work = tmp_path / "delta-work"
    bundle_delta.make(old_app, new_app, work)
    if corrupt:
        manifest = json.loads((work / "manifest.json").read_text())
        manifest["format"] = 99
        (work / "manifest.json").write_text(json.dumps(manifest))
    delta = tmp_path / bundle_delta.delta_name("0.5.1", "0.5.2")
    subprocess.run(["ditto", "-c", "-k", str(work), str(delta)], check=True)
    digest = hashlib.sha256(delta.read_bytes()).hexdigest()
    (tmp_path / updater.checksum_name(delta.name)).write_text(f"{digest}  {delta.name}\n")
    return updater.Release(**{**release.__dict__, "delta": updater.Asset(
        delta.name, delta.as_uri(), delta.stat().st_size, (tmp_path / updater.checksum_name(delta.name)).as_uri())})


def test_an_update_downloads_only_the_delta_when_one_is_published(tmp_path, codesign):
    old, new = two_versions(tmp_path)
    installed = (tmp_path / "Applications").mkdir() or old.rename(tmp_path / "Applications" / "Maramax.app")
    release = published_with_delta(tmp_path, installed, new)
    # A copy installed from a browser download carries quarantine on files the delta leaves alone.
    unchanged = installed / "Contents" / "Resources" / "lib" / "big.bin"
    subprocess.run(["xattr", "-w", "com.apple.quarantine", "0081;00000000;Safari;", str(unchanged)], check=True)
    fetched = []
    staged = updater.download(release, "0.5.1", installed, tmp_path / "staging",
                              lambda received, expected: fetched.append(expected), lambda: False)
    assert set(fetched) == {release.delta.size}
    assert bundle_delta.entries(staged) == bundle_delta.entries(new)
    assert version_of(installed) == "0.5.1"    # The installed app was only copied.
    assert not (tmp_path / "staging").exists()
    # The rebuilt app, not the installed one, passed the pinned signature check.
    assert codesign == [["codesign", "--verify", "--deep", "--strict",
                         f"-R={updater.signer_requirement('com.maramax.dictation')}",
                         str(tmp_path / "staging" / "assembled" / "Maramax.app")]]
    attributes = subprocess.run(["xattr", "-r", str(staged)], capture_output=True, text=True, check=True).stdout
    assert "com.apple.quarantine" not in attributes


def test_a_delta_that_fails_falls_back_to_the_whole_app(tmp_path, codesign):
    old, new = two_versions(tmp_path)
    installed = (tmp_path / "Applications").mkdir() or old.rename(tmp_path / "Applications" / "Maramax.app")
    release = published_with_delta(tmp_path, installed, new, corrupt=True)
    fetched = []
    staged = updater.download(release, "0.5.1", installed, tmp_path / "staging",
                              lambda received, expected: fetched.append(expected), lambda: False)
    assert release.archive.size in fetched and version_of(staged) == "0.5.2"


def test_a_broken_delta_asset_still_leaves_the_whole_app_to_install():
    names = ("Maramax-0.5.2.zip", "Maramax-0.5.2.zip.sha256", "Maramax-0.5.2-from-0.5.1.delta")  # No .sha256.
    release = updater.newer_release("0.5.1", release_payload(assets=names))
    assert release.delta is None and release.archive.name == "Maramax-0.5.2.zip"


def test_a_cancelled_download_leaves_nothing_behind(tmp_path, codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    with pytest.raises(updater.UpdateCancelled):
        updater.download(published(tmp_path), "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: True)
    assert not (tmp_path / "staging").exists() and not (installed.parent / updater.STAGED_NAME).exists()


def test_skipped_version_and_update_checks_persist(tmp_path):
    path = tmp_path / "settings.json"
    AppConfig(check_for_updates=False, skipped_update_version="0.5.2").save(path)
    loaded = AppConfig.load(path)
    assert loaded.check_for_updates is False and loaded.skipped_update_version == "0.5.2"
    assert AppConfig().check_for_updates is True


# -- A delta made by someone without the signing key touches nothing outside the app --

def victim(tmp_path):
    """A file of the user's outside the app, as a hostile delta would aim at it."""
    path = tmp_path / "victim"
    path.write_text("private")
    path.chmod(0o600)
    return path


def untouched(path):
    return path.stat().st_mode & 0o7777 == 0o600 and path.read_text() == "private"


@pytest.mark.parametrize("link, alias, kind, message", [
    ("Contents/x", "Contents/./x", "file", "spells a path"),       # pathlib reads both as Contents/x
    ("Contents/x", "Contents//x", "file", "spells a path"),
    ("Contents/x", "Contents/X", "dir", "names one entry twice"),  # APFS ignores case...
    ("Contents/é", "Contents/é", "file", "names one entry twice"),  # ...and Unicode normalization
])
def test_a_delta_cannot_set_permissions_through_a_link_it_carries_under_another_name(tmp_path, link, alias,
                                                                                       kind, message):
    old, new = two_versions(tmp_path)
    delta = tmp_path / "delta"
    bundle_delta.make(old, new, delta)
    target = victim(tmp_path)
    (delta / "files" / link).symlink_to(target)
    manifest = json.loads((delta / "manifest.json").read_text())
    manifest["carried"].append(link)
    manifest["tree"][alias] = [kind, "" if kind == "dir" else "0" * 64, 0o777]
    manifest["tree"][link] = ["link", str(target), 0]
    (delta / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(bundle_delta.DeltaError) as refused:
        apply_to_a_clone(tmp_path, old, delta)
    assert untouched(target)
    assert message in str(refused.value)    # Refused before anything was changed, not only found out after.


def test_permissions_are_never_set_through_a_symlink(tmp_path):
    """Whatever the tree calls a link that is on disk, chmod is not sent through it."""
    old, new = two_versions(tmp_path)
    delta = tmp_path / "delta"
    bundle_delta.make(old, new, delta)
    target = victim(tmp_path)
    (old / "Contents" / "Resources" / "pointer").symlink_to(target)  # In the copy the delta is applied to.
    manifest = json.loads((delta / "manifest.json").read_text())
    manifest["tree"]["Contents/Resources/pointer"] = ["file", "0" * 64, 0o777]   # Not carried: left in place.
    (delta / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(bundle_delta.DeltaError) as refused:
        apply_to_a_clone(tmp_path, old, delta)
    assert untouched(target)
    assert "permissions through a symlink" in str(refused.value)


def test_an_installed_update_is_writable_only_by_its_owner(tmp_path, codesign):
    """The signature does not cover permission bits; the archive sets them, so they are restricted."""
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    release = published(tmp_path, modes={"Contents/MacOS/Maramax": 0o6777, "Contents": 0o777})
    staged = updater.download(release, "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: False)
    modes = {path: path.stat().st_mode & 0o7777 for path in [staged, *staged.rglob("*")]}
    assert modes[staged / "Contents" / "MacOS" / "Maramax"] == 0o755 and modes[staged / "Contents"] == 0o755
    assert not [path for path, mode in modes.items() if mode & 0o6022]


def test_restricting_a_rebuilt_app_leaves_what_its_links_point_to_alone(tmp_path, codesign):
    old, new = two_versions(tmp_path)
    target = victim(tmp_path)
    target.chmod(0o666)
    (new / "Contents" / "Resources" / "elsewhere").symlink_to(target)
    (new / "Contents" / "Resources" / "added.txt").chmod(0o664)   # As the bundle's own PythonApplet.icns is.
    installed = (tmp_path / "Applications").mkdir() or old.rename(tmp_path / "Applications" / "Maramax.app")
    staged = updater.download(published_with_delta(tmp_path, installed, new), "0.5.1", installed,
                              tmp_path / "staging", lambda *a: None, lambda: False)
    assert (staged / "Contents" / "Resources" / "added.txt").stat().st_mode & 0o777 == 0o644
    assert target.stat().st_mode & 0o777 == 0o666 and target.read_text() == "private"


def test_an_info_plist_that_is_not_a_dictionary_is_refused_and_cleared(tmp_path, codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    release = published(tmp_path, info_plist=plistlib.dumps(["not", "a", "dictionary"]))
    with pytest.raises(updater.UpdateError, match="not a dictionary"):
        updater.download(release, "0.5.1", installed, tmp_path / "staging", lambda *a: None, lambda: False)
    assert not (tmp_path / "staging").exists() and codesign == []


# -- The delta's signature check, and its fallback --

def test_a_rebuilt_app_that_fails_the_signature_check_falls_back_to_the_whole_app(tmp_path, monkeypatch):
    real = updater._run
    verified = []

    def run(command, what):
        if command[0] != "codesign":
            return real(command, what)
        verified.append(command[-1])
        if "assembled" in command[-1]:
            raise updater.UpdateError(f"Could not {what}: a sealed resource is missing or invalid")
    monkeypatch.setattr(updater, "_run", run)
    old, new = two_versions(tmp_path)
    installed = (tmp_path / "Applications").mkdir() or old.rename(tmp_path / "Applications" / "Maramax.app")
    release = published_with_delta(tmp_path, installed, new)
    fetched = []
    staged = updater.download(release, "0.5.1", installed, tmp_path / "staging",
                              lambda received, expected: fetched.append(expected), lambda: False)
    assert verified == [str(tmp_path / "staging" / "assembled" / "Maramax.app"),
                        str(tmp_path / "staging" / "unpacked" / "Maramax-0.5.2" / "Maramax.app")]
    assert release.archive.size in fetched
    assert bundle_delta.entries(staged) != bundle_delta.entries(new)   # The archive's app, not the rebuilt one.


def test_a_retry_replaces_a_new_app_left_beside_the_installed_one(tmp_path, codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    left = fake_bundle(tmp_path / "earlier", version="0.5.0").rename(installed.parent / updater.STAGED_NAME)
    (left / "Contents" / "Resources").mkdir()
    (left / "Contents" / "Resources" / "stale.txt").write_text("from an earlier attempt")
    staged = updater.download(published(tmp_path), "0.5.1", installed, tmp_path / "staging",
                              lambda *a: None, lambda: False)
    assert staged == left and version_of(staged) == "0.5.2"
    assert not (staged / "Contents" / "Resources" / "stale.txt").exists()


# -- Cancelling a download that has stalled --

def test_a_cancel_during_a_failing_delta_stops_instead_of_fetching_the_whole_app(tmp_path, codesign, monkeypatch):
    old, new = two_versions(tmp_path)
    installed = (tmp_path / "Applications").mkdir() or old.rename(tmp_path / "Applications" / "Maramax.app")
    release = published_with_delta(tmp_path, installed, new, corrupt=True)
    opened, fetched = [], []
    real_open = updater._open
    monkeypatch.setattr(updater, "_open", lambda url, version: opened.append(url) or real_open(url, version))

    def progress(received, expected):
        fetched.append(expected)     # Cancel is pressed while the delta is being read.
    with pytest.raises(updater.UpdateCancelled):
        updater.download(release, "0.5.1", installed, tmp_path / "staging", progress, lambda: bool(fetched))
    assert not {release.archive.url, release.archive.checksum_url} & set(opened)
    assert not (tmp_path / "staging").exists()


def worker_reports(monkeypatch, controller, download):
    """Run _download_worker with `download` in place of updater.download;
    returns what it handed to the main thread, undelivered."""
    handed = []
    monkeypatch.setattr(update_offer.AppHelper, "callAfter", lambda function, *args: handed.append((function, args)))
    monkeypatch.setattr(update_offer.updater, "download", download)
    release = a_release()
    controller._release = release
    controller._set_step(update_offer.Step.DOWNLOADING, 0)
    controller._download_worker(release, controller._paths.updates / "Maramax.app", controller._cancel)
    return handed


def test_a_cancelled_download_that_then_times_out_is_a_stop_not_a_failure(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    controller._window = FakeWindow()

    def stalled(release, current, installed, staging, progress, cancelled):
        controller.cancel_requested()            # The user gives up on a stalled network...
        assert item.title == "Cancelling the Update…" and not controller.can_check()
        raise updater.UpdateError("Could not download Maramax-0.5.2.zip: timed out")   # ...then the read does.
    for function, args in worker_reports(monkeypatch, controller, stalled):
        function(*args)
    assert alerts == [] and item.title == "Install Maramax 0.5.2…" and controller.can_check()


def test_a_download_failure_without_a_cancel_is_reported(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)

    def failing(*args):
        raise updater.UpdateError("does not match its published SHA-256")
    for function, args in worker_reports(monkeypatch, controller, failing):
        function(*args)
    assert alerts[0]["title"] == "Maramax 0.5.2 could not be installed"


def test_download_progress_reaches_the_main_thread_once_per_percent(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    size = 200_000

    def download(release, current, installed, staging, progress, cancelled):
        for received in range(0, size, 1000):     # 200 blocks: two per percent.
            progress(received, size)
        progress(size, size)
        return staging / "Maramax.app"
    handed = worker_reports(monkeypatch, controller, download)
    reports = [args for function, args in handed if function == controller._show_download_progress]
    assert [received * 100 // expected for received, expected in reports] == list(range(101))
    assert reports[-1] == (size, size)


# -- Offering --

def test_check_now_asks_again_even_with_a_release_already_known(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(update_offer.threading, "Thread",
                        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["args"])))
    controller._checked(a_release(), asked=False)
    controller._answered(update_prompt.Choice.LATER)
    asked.clear()
    controller.check_requested()
    # A fresh check (asked=True), which offers what the release page says now.
    assert started == [(True,)] and asked == [] and item.title == "Checking for Updates…"


def test_a_prompt_window_that_cannot_be_made_does_not_stop_the_checks(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(update_offer.threading, "Thread",
                        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["args"])))

    def broken():
        raise RuntimeError("no window today")
    monkeypatch.setattr(update_offer, "UpdatePromptWindow", SimpleNamespace(alloc=broken))
    with pytest.raises(RuntimeError, match="no window today"):
        controller._checked(a_release(), asked=False)
    controller.check_requested()          # Not stuck in PROMPTING with no window to withdraw.
    assert started == [(True,)] and item.title == "Checking for Updates…"


def test_an_automatic_prompt_waits_while_a_dialog_is_open(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path, modal=object())
    controller._checked(a_release(), asked=False)
    assert asked == [] and item.title == "Install Maramax 0.5.2…"
    controller._checked(a_release(), asked=True)    # One the user asked for still answers.
    assert asked == ["0.5.2"]


def test_a_new_app_left_staged_by_an_earlier_run_is_removed_after_launch(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    later, tick = run_timers(monkeypatch)
    discarded = []
    monkeypatch.setattr(update_offer.UpdateOffer, "_discard", staticmethod(discarded.append))
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    controller._installed_app = installed
    fake_bundle(tmp_path / "earlier").rename(updater.staged_app(installed))
    controller.start()
    assert tick() == update_offer.LEFTOVER_REMOVAL_SECONDS and discarded == [updater.staged_app(installed)]
    # One this run has staged and is waiting to install is not a leftover.
    discarded.clear()
    controller._set_step(update_offer.Step.INSTALLING)
    controller._remove_leftover()
    assert discarded == []


def test_numbered_items_line_up_on_their_dots_and_their_text():
    """The release notes as styled text, built in their own process."""
    body = r'''
from AppKit import NSTextAlignmentLeft, NSTextAlignmentRight
from parakeet_dictation.update_prompt import NUMBER_END, NUMBER_INDENT, note_blocks, rendered_notes
text = rendered_notes(note_blocks("9. Ninth\n10. Tenth\n\nDone."))
assert str(text.string()) == "\t9.\tNinth\n\t10.\tTenth\nDone.", repr(str(text.string()))
style = text.attribute_atIndex_effectiveRange_("NSParagraphStyle", 0, None)[0]
stops = [(stop.alignment(), stop.location()) for stop in style.tabStops()]
assert stops == [(NSTextAlignmentRight, NUMBER_END), (NSTextAlignmentLeft, NUMBER_INDENT)], stops
assert style.headIndent() == NUMBER_INDENT     # A wrapped line starts under the item's text.
'''
    result = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1500:]


def test_the_wait_for_idle_can_still_be_cancelled_after_the_restart_notice_gave_way():
    """A real progress window, built off-screen in its own process."""
    body = r'''
from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
from parakeet_dictation.update_window import UpdateProgressWindow
window = UpdateProgressWindow.alloc().initWithCancel_(lambda: None)
window.show_progress(5, 10)
assert window.bar.doubleValue() == 0.5
window.show_progress(10, 10)           # Downloaded: the bar is full from here to the restart.
assert window.bar.doubleValue() == 1.0 and not window.bar.isIndeterminate()
window.show_restarting()
assert not window.cancel.isEnabled()
assert window.bar.doubleValue() == 1.0 and not window.bar.isIndeterminate()
window.show_ready("0.5.2", True)       # A dictation began during "Restarting Maramax…".
assert window.cancel.isEnabled()
assert window.bar.doubleValue() == 1.0 and not window.bar.isIndeterminate()
'''
    result = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1500:]
