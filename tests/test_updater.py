"""Finding, verifying, and swapping in a new release, with file URLs and a fake bundle. No network, no app is opened."""
import hashlib
import io
import json
import os
import plistlib
import subprocess
import time
import urllib.error
import zipfile
from types import SimpleNamespace

import pytest

from parakeet_dictation import update_offer, updater
from parakeet_dictation.config import AppConfig


@pytest.fixture(autouse=True)
def no_real_dialogs(monkeypatch):
    """A test that reaches a real alert would put it on the screen and wait for a click."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to show a real dialog")
    monkeypatch.setattr(update_offer.rumps, "alert", refuse)
    monkeypatch.setattr(update_offer, "NSAlert", SimpleNamespace(alloc=refuse))


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
    assert release.archive_url.endswith(".zip") and release.checksum_url.endswith(".zip.sha256")
    assert release.notes == "Fixes." and release.archive_size == 2048


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
    (release_payload(assets=("Maramax-0.5.2.zip",)), "one .zip with a matching"),
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


def published(tmp_path, identifier="com.maramax.dictation", version="0.5.2", tamper=False):
    """A release archive laid out as create_release.py makes it, served from files."""
    source = tmp_path / "source"
    fake_bundle(source / f"Maramax-{version}", identifier, version)
    archive = tmp_path / f"Maramax-{version}.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in sorted(source.rglob("*")):
            zipped.write(path, path.relative_to(source))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if tamper:
        archive.write_bytes(archive.read_bytes() + b"x")
    checksum = tmp_path / f"{archive.name}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n")
    return updater.Release(version=version, notes="", page_url="", archive_url=archive.as_uri(),
                           archive_size=archive.stat().st_size, checksum_url=checksum.as_uri())


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
    staged = updater.download(published(tmp_path), "0.5.1", installed, tmp_path / "staging", progress.append)
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
        updater.download(published(tmp_path, **kwargs), "0.5.1", installed, tmp_path / "staging", lambda f: None)
    assert codesign == []
    assert not (tmp_path / "staging").exists()
    assert not (installed.parent / updater.STAGED_NAME).exists()


def test_a_download_whose_app_reports_another_version_is_refused(tmp_path, codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    release = published(tmp_path, version="0.5.2")
    mislabelled = updater.Release(**{**release.__dict__, "version": "0.5.3"})
    with pytest.raises(updater.UpdateError, match="says it is version 0.5.2"):
        updater.download(mislabelled, "0.5.1", installed, tmp_path / "staging", lambda f: None)


def test_a_signature_from_another_certificate_is_refused(tmp_path, monkeypatch):
    real = updater._run

    def unsigned(command, what):
        if command[0] == "codesign":
            raise updater.UpdateError(f"Could not {what}: test-requirement: code failed to satisfy")
        real(command, what)
    monkeypatch.setattr(updater, "_run", unsigned)
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    with pytest.raises(updater.UpdateError, match="release certificate"):
        updater.download(published(tmp_path), "0.5.1", installed, tmp_path / "staging", lambda f: None)
    assert not (installed.parent / updater.STAGED_NAME).exists()


def test_the_kept_previous_copy_and_read_only_folders_are_refused_before_downloading(tmp_path):
    updates = tmp_path / "updates"
    kept = fake_bundle(updates / "previous", version="0.5.0")
    with pytest.raises(updater.UpdateError, match="kept from the last update"):
        updater.check_installable(kept, updates)
    locked = tmp_path / "Locked"
    app = fake_bundle(locked)
    os.chmod(locked, 0o555)
    try:
        with pytest.raises(updater.UpdateError, match="not writable"):
            updater.check_installable(app, updates)
    finally:
        os.chmod(locked, 0o755)
    updater.check_installable(fake_bundle(tmp_path / "Applications"), updates)


# -- The swap after quitting --

def run_swap(tmp_path, *, staged, pid=None, failing_mv_source=None):
    """Run the real script with `open` (and optionally `mv`) replaced."""
    installed = tmp_path / "Applications" / "Maramax.app"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    opened = tmp_path / "opened.txt"
    (fake_bin / "open").write_text(f'#!/bin/sh\necho "$1" >> "{opened}"\n')
    (fake_bin / "open").chmod(0o755)
    if failing_mv_source is not None:
        (fake_bin / "mv").write_text(f'#!/bin/sh\n[ "$1" = "{failing_mv_source}" ] && exit 1\nexec /bin/mv "$@"\n')
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
    installed, result, opened = run_swap(tmp_path, staged=staged, failing_mv_source=str(staged))
    assert result is updater.InstallResult.NOT_PLACED and version_of(installed) == "0.5.1"
    assert opened == [str(installed)]


def test_a_swap_that_cannot_move_the_old_app_aside_changes_nothing(tmp_path):
    staged = staged_beside(tmp_path)
    installed = tmp_path / "Applications" / "Maramax.app"
    _, result, opened = run_swap(tmp_path, staged=staged, failing_mv_source=str(installed))
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


def test_the_menu_item_says_what_is_happening():
    title = update_offer.menu_title
    assert title(update_offer.Step.IDLE, None) == update_offer.CHECK_TITLE
    assert title(update_offer.Step.IDLE, "0.5.2") == "Install Maramax 0.5.2…"
    assert title(update_offer.Step.DOWNLOADING, "0.5.2", 40) == "Downloading Maramax 0.5.2… 40%"


@pytest.mark.parametrize("step, version, checked, problem, updated_to, expected", [
    (update_offer.Step.IDLE, None, False, None, None, "Not checked yet."),
    (update_offer.Step.IDLE, None, True, None, None, "This is the newest version."),
    (update_offer.Step.IDLE, None, False, "Could not reach GitHub", None,
     "The last check did not work: Could not reach GitHub"),
    (update_offer.Step.IDLE, "0.6.1", True, None, None, "Maramax 0.6.1 is available."),
    (update_offer.Step.IDLE, None, True, None, "0.6.1", "Updated to Maramax 0.6.1."),
    (update_offer.Step.CHECKING, None, True, None, None, "Checking for updates…"),
    (update_offer.Step.INSTALLING, "0.6.1", True, None, None,
     "Maramax 0.6.1 is ready and installs as soon as Maramax is idle."),
])
def test_settings_says_where_updates_stand(step, version, checked, problem, updated_to, expected):
    assert update_offer.status_line(step=step, version=version, percent=None, checked=checked, problem=problem,
                                    updated_to=updated_to) == expected


@pytest.mark.parametrize("size, expected", [
    (2048, "2 KB"), (100, "1 KB"), (840 * 1024, "840 KB"), (210_252_034, "201 MB"), (1024 * 1024, "1 MB"),
])
def test_download_sizes_read_naturally(size, expected):
    assert update_offer.download_size(size) == expected


def test_every_failed_install_has_something_to_say():
    for result in updater.InstallResult:
        message = update_offer.install_failure(result)
        assert (message is None) is (result is updater.InstallResult.INSTALLED)


def test_release_notes_are_shown_without_markdown():
    notes = "## New\n**Updates itself.** See [the guide](https://x.test) and `START HERE.md`."
    assert update_offer.plain_notes(notes) == "New\nUpdates itself. See the guide and START HERE.md."


def offer(monkeypatch, tmp_path, answer=None, busy=False):
    alerts = []
    monkeypatch.setattr(update_offer.rumps, "alert", lambda **kwargs: alerts.append(kwargs) or 1)
    monkeypatch.setattr(update_offer, "NSApplication",
                        SimpleNamespace(sharedApplication=lambda: SimpleNamespace(modalWindow=lambda: None)))
    item = SimpleNamespace(title=update_offer.CHECK_TITLE)
    config = AppConfig()
    saved = []
    controller = update_offer.UpdateOffer(
        menu_item=item, current_version="0.5.1", installed_app=None, updates_dir=tmp_path / "updates",
        log_path=tmp_path / "update.log", config=config, save_settings=lambda: saved.append(True) or True,
        is_busy=lambda: busy, quit_app=lambda: None, on_change=lambda: None)
    asked = []
    controller._ask = lambda release: asked.append(release.version) or answer
    return controller, item, config, alerts, saved, asked


def test_skipping_a_version_is_remembered(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path,
                                                          answer=update_offer.NSAlertThirdButtonReturn)
    release = updater.newer_release("0.5.1", release_payload())
    controller._checked(release, None, asked=False)
    assert asked == ["0.5.2"]
    assert config.skipped_update_version == "0.5.2" and saved
    assert item.title == "Install Maramax 0.5.2…"
    controller._checked(release, None, asked=False)   # The next daily check stays quiet.
    assert asked == ["0.5.2"]


def test_an_automatic_check_never_alerts_about_a_failure_or_no_update(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    controller._checked(None, "Could not reach GitHub", asked=False)
    controller._checked(None, None, asked=False)
    assert alerts == [] and asked == [] and item.title == update_offer.CHECK_TITLE


def test_a_requested_check_reports_that_the_app_is_current(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    controller._checked(None, None, asked=True)
    assert alerts[0]["title"] == "Maramax is up to date"


def test_a_failed_check_is_shown_until_one_succeeds(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    changes = []
    controller._on_change = lambda: changes.append(controller.status_text())
    controller._checked(None, "Could not reach GitHub", asked=False)
    assert changes[-1] == "The last check did not work: Could not reach GitHub"
    controller._checked(None, None, asked=False)
    assert changes[-1] == "This is the newest version." and controller.can_check()


def test_an_install_waits_for_two_idle_looks_then_quits(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    busy = [True]
    controller._is_busy = lambda: busy[0]
    later, installs, quits = [], [], []
    monkeypatch.setattr(update_offer, "call_later", lambda delay, function, *args: later.append((function, args)))
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    controller._quit_app = lambda: quits.append(True)
    release = updater.newer_release("0.5.1", release_payload())
    controller._release = release
    controller._downloaded(release, tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME, None)
    assert item.title == "Installing Maramax 0.5.2…"

    def tick():
        function, args = later.pop(0)
        function(*args)

    tick()                      # Still dictating.
    busy[0] = False
    tick()                      # Idle once: a finished dictation may still be pasting.
    assert not installs
    tick()                      # Idle twice in a row.
    assert installs[0]["previous_app"] == tmp_path / "updates" / "previous" / "Maramax.app"
    assert installs[0]["result_path"] == tmp_path / "updates" / "last-install" and quits


def test_an_open_dialog_holds_the_install(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    monkeypatch.setattr(update_offer, "NSApplication", SimpleNamespace(
        sharedApplication=lambda: SimpleNamespace(modalWindow=lambda: object())))
    later, installs = [], []
    monkeypatch.setattr(update_offer, "call_later", lambda delay, function, *args: later.append((function, args)))
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    controller._install_when_idle(tmp_path / "Maramax.app", tmp_path / updater.STAGED_NAME, idle_before=True)
    assert not installs and later


def test_a_failed_download_is_reported_and_the_offer_stays(monkeypatch, tmp_path):
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    release = updater.newer_release("0.5.1", release_payload())
    controller._release = release
    controller._downloaded(release, tmp_path / "Maramax.app", None, "does not match its published SHA-256")
    assert "SHA-256" in alerts[0]["message"]
    assert item.title == "Install Maramax 0.5.2…"


def test_the_next_launch_reports_how_the_last_update_went(monkeypatch, tmp_path):
    later = []
    monkeypatch.setattr(update_offer, "call_later", lambda delay, function, *args: later.append((delay, function)))
    controller, item, config, alerts, saved, asked = offer(monkeypatch, tmp_path)
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates" / "last-install").write_text("installed")
    controller.start()
    assert controller.status_text() == "Updated to Maramax 0.5.1."
    (tmp_path / "updates" / "last-install").write_text("not-placed")
    controller.start()
    alert = next(function for delay, function in later if delay == 5)
    alert()
    assert alerts[0]["title"] == "The update was not installed" and "restored" in alerts[0]["message"]


def test_skipped_version_and_update_checks_persist(tmp_path):
    path = tmp_path / "settings.json"
    AppConfig(check_for_updates=False, skipped_update_version="0.5.2").save(path)
    loaded = AppConfig.load(path)
    assert loaded.check_for_updates is False and loaded.skipped_update_version == "0.5.2"
    assert AppConfig().check_for_updates is True
