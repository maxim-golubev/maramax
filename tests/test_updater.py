"""Finding, verifying, and swapping in a new release, with file URLs and a fake bundle. No network, no app is opened."""
import hashlib
import io
import os
import plistlib
import subprocess
import urllib.error
import zipfile
from types import SimpleNamespace

import pytest

from parakeet_dictation import update_offer, updater
from parakeet_dictation.config import AppConfig


def release_payload(tag="v0.5.2", assets=("Maramax-0.5.2.zip", "Maramax-0.5.2.zip.sha256"), body="Fixes."):
    return {
        "tag_name": tag, "body": body, "html_url": f"https://github.com/x/maramax/releases/tag/{tag}",
        "assets": [{"name": name, "size": 2048, "browser_download_url": f"https://example.test/{name}"} for name in assets],
    }


@pytest.mark.parametrize("tag, expected", [("v0.5.2", "0.5.2"), ("0.6", "0.6"), ("v0.10.0", "0.10.0")])
def test_a_newer_tag_is_an_update(tag, expected):
    release = updater.newer_release("0.5.1", release_payload(tag=tag))
    assert release is not None and release.version == expected
    assert release.archive_url.endswith(".zip") and release.checksum_url.endswith(".zip.sha256")
    assert release.notes == "Fixes."


@pytest.mark.parametrize("tag", ["v0.5.1", "v0.5.0", "0.4.9", "v0.5"])
def test_the_same_or_an_older_tag_is_not(tag):
    assert updater.newer_release("0.5.1", release_payload(tag=tag)) is None


def test_versions_compare_as_numbers_not_text():
    assert updater.version_key("0.10.0") > updater.version_key("0.9.9")


@pytest.mark.parametrize("payload, message", [
    (release_payload(tag="nightly"), "not a version number"),
    (release_payload(assets=("Maramax-0.5.2.zip",)), "one .zip with a matching"),
    ({"assets": []}, "no tag"),
])
def test_a_malformed_release_is_reported_not_ignored(payload, message):
    with pytest.raises(updater.UpdateError, match=message):
        updater.newer_release("0.5.1", payload)


def test_no_published_release_is_no_update(monkeypatch):
    def not_found(url, version):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO())
    monkeypatch.setattr(updater, "_open", not_found)
    assert updater.latest_release("0.5.1") is None


def test_an_unreachable_server_is_an_error_that_says_so(monkeypatch):
    def offline(url, version):
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(updater, "_open", offline)
    with pytest.raises(updater.UpdateError, match="Could not reach GitHub"):
        updater.latest_release("0.5.1")


def test_the_latest_release_is_read_from_the_api(tmp_path):
    answer = tmp_path / "latest.json"
    answer.write_text(__import__("json").dumps(release_payload()))
    release = updater.latest_release("0.5.1", url=answer.as_uri())
    assert release is not None and release.version == "0.5.2"


def test_checksum_files_are_parsed_strictly():
    digest = "a" * 64
    assert updater.parse_checksum(f"{digest}  Maramax-0.5.2.zip\n") == digest
    with pytest.raises(updater.UpdateError):
        updater.parse_checksum("not a digest")


# -- Download --

def fake_bundle(root, identifier="com.maramax.dictation", version="0.5.2"):
    app = root / "Maramax.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": identifier, "CFBundleShortVersionString": version}))
    (app / "Contents" / "MacOS" / "Maramax").write_text("#!/bin/sh\n")
    return app


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
def no_codesign(monkeypatch):
    real = updater._run
    signed = []
    monkeypatch.setattr(updater, "_run", lambda command, what: signed.append(command[-1]) if command[0] == "codesign"
                        else real(command, what))
    return signed


def test_a_verified_download_is_unpacked_ready_to_install(tmp_path, no_codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    progress = []
    new_app = updater.download(published(tmp_path), "0.5.1", installed, tmp_path / "staging", progress.append)
    assert new_app.name == "Maramax.app" and (new_app / "Contents" / "Info.plist").is_file()
    assert progress[-1] == 1.0
    assert no_codesign == [str(new_app)]
    assert not (tmp_path / "staging" / "update.zip").exists()


@pytest.mark.parametrize("kwargs, message", [
    ({"tamper": True}, "does not match its published SHA-256"),
    ({"identifier": "com.example.other"}, "com.example.other"),
])
def test_a_download_that_is_not_this_app_is_refused(tmp_path, no_codesign, kwargs, message):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    with pytest.raises(updater.UpdateError, match=message):
        updater.download(published(tmp_path, **kwargs), "0.5.1", installed, tmp_path / "staging", lambda f: None)
    assert no_codesign == []


def test_a_download_whose_app_reports_another_version_is_refused(tmp_path, no_codesign):
    installed = fake_bundle(tmp_path / "Applications", version="0.5.1")
    release = published(tmp_path, version="0.5.2")
    mislabelled = updater.Release(**{**release.__dict__, "version": "0.5.3"})
    with pytest.raises(updater.UpdateError, match="says it is version 0.5.2"):
        updater.download(mislabelled, "0.5.1", installed, tmp_path / "staging", lambda f: None)


# -- The swap after quitting --

def run_swap(tmp_path, new_app):
    installed = tmp_path / "Applications" / "Maramax.app"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    opened = tmp_path / "opened.txt"
    (fake_bin / "open").write_text(f'#!/bin/sh\necho "$1" >> "{opened}"\n')
    (fake_bin / "open").chmod(0o755)
    exited = subprocess.Popen(["true"])
    exited.wait()
    script = tmp_path / "install.sh"
    script.write_text(updater.swap_script(
        pid=exited.pid, new_app=new_app, installed_app=installed,
        previous_app=tmp_path / "updates" / "previous" / "Maramax.app",
        staging=tmp_path / "updates" / "download", log_path=tmp_path / "logs" / "update.log"))
    (tmp_path / "logs").mkdir(exist_ok=True)
    subprocess.run(["/bin/sh", str(script)], env={"PATH": f"{fake_bin}:/usr/bin:/bin"}, check=False, timeout=30)
    return installed, opened.read_text().splitlines()


def test_the_swap_installs_the_new_app_and_keeps_the_old_one(tmp_path):
    fake_bundle(tmp_path / "Applications", version="0.5.1")
    new_app = fake_bundle(tmp_path / "updates" / "download" / "unpacked" / "Maramax-0.5.2")
    installed, opened = run_swap(tmp_path, new_app)
    assert plistlib.loads((installed / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"] == "0.5.2"
    previous = tmp_path / "updates" / "previous" / "Maramax.app" / "Contents" / "Info.plist"
    assert plistlib.loads(previous.read_bytes())["CFBundleShortVersionString"] == "0.5.1"
    assert not (tmp_path / "updates" / "download").exists()
    assert opened == [str(installed)]


def test_a_swap_that_cannot_place_the_new_app_restores_the_old_one(tmp_path):
    fake_bundle(tmp_path / "Applications", version="0.5.1")
    installed, opened = run_swap(tmp_path, tmp_path / "missing" / "Maramax.app")
    assert plistlib.loads((installed / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"] == "0.5.1"
    assert opened == [str(installed)]
    assert "restoring the previous version" in (tmp_path / "logs" / "update.log").read_text()


def test_paths_with_spaces_and_quotes_survive_the_script(tmp_path):
    root = tmp_path / "Application Support" / "it's here"
    root.mkdir(parents=True)
    fake_bundle(root / "Applications", version="0.5.1")
    new_app = fake_bundle(root / "updates" / "download" / "unpacked" / "Maramax-0.5.2")
    installed, opened = run_swap(root, new_app)
    assert plistlib.loads((installed / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"] == "0.5.2"


def test_an_unwritable_install_location_is_refused_before_quitting(tmp_path):
    locked = tmp_path / "Applications"
    locked.mkdir()
    os.chmod(locked, 0o555)
    try:
        with pytest.raises(updater.UpdateError, match="not writable"):
            updater.install_after_exit(new_app=tmp_path / "new.app", installed_app=locked / "Maramax.app",
                                       previous_app=tmp_path / "previous.app", staging=tmp_path,
                                       log_path=tmp_path / "update.log", pid=os.getpid())
    finally:
        os.chmod(locked, 0o755)


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


def offer(monkeypatch, answers, busy=False):
    alerts = []
    monkeypatch.setattr(update_offer.rumps, "alert", lambda **kwargs: alerts.append(kwargs) or answers.pop(0))
    item = SimpleNamespace(title=update_offer.CHECK_TITLE)
    config = AppConfig()
    saved = []
    controller = update_offer.UpdateOffer(
        menu_item=item, current_version="0.5.1", installed_app=None, updates_dir=None, log_path=None,
        config=config, save_settings=lambda: saved.append(True) or True, is_busy=lambda: busy, quit_app=lambda: None)
    return controller, item, config, alerts, saved


def test_skipping_a_version_is_remembered(monkeypatch):
    controller, item, config, alerts, saved = offer(monkeypatch, answers=[-1])
    release = updater.newer_release("0.5.1", release_payload())
    controller._checked(release, None, asked=False)
    assert alerts[0]["title"] == "Maramax 0.5.2 is available"
    assert config.skipped_update_version == "0.5.2" and saved
    assert item.title == "Install Maramax 0.5.2…"
    controller._checked(release, None, asked=False)   # The next daily check stays quiet.
    assert len(alerts) == 1


def test_an_automatic_check_never_alerts_about_a_failure_or_no_update(monkeypatch):
    controller, item, config, alerts, saved = offer(monkeypatch, answers=[])
    controller._checked(None, "Could not reach GitHub", asked=False)
    controller._checked(None, None, asked=False)
    assert alerts == [] and item.title == update_offer.CHECK_TITLE


def test_a_requested_check_reports_that_the_app_is_current(monkeypatch):
    controller, item, config, alerts, saved = offer(monkeypatch, answers=[1])
    controller._checked(None, None, asked=True)
    assert alerts[0]["title"] == "Maramax is up to date"


def test_an_install_waits_for_two_idle_looks_then_quits(monkeypatch, tmp_path):
    controller, item, config, alerts, saved = offer(monkeypatch, answers=[])
    busy = [True]
    controller._is_busy = lambda: busy[0]
    controller._updates_dir = tmp_path / "updates"
    later, installs, quits = [], [], []
    monkeypatch.setattr(update_offer, "call_later", lambda delay, function, *args: later.append((function, args)))
    monkeypatch.setattr(update_offer.updater, "install_after_exit", lambda **kwargs: installs.append(kwargs))
    controller._quit_app = lambda: quits.append(True)
    release = updater.newer_release("0.5.1", release_payload())
    controller._release = release
    controller._downloaded(release, tmp_path / "Maramax.app", tmp_path / "new" / "Maramax.app", None)
    assert item.title == "Installing Maramax 0.5.2…"

    def tick():
        function, args = later.pop(0)
        function(*args)

    tick()                      # Still dictating.
    busy[0] = False
    tick()                      # Idle once: a finished dictation may still be pasting.
    assert not installs
    tick()                      # Idle twice in a row.
    assert installs[0]["previous_app"] == tmp_path / "updates" / "previous" / "Maramax.app" and quits


def test_a_failed_download_is_reported_and_the_offer_stays(monkeypatch, tmp_path):
    controller, item, config, alerts, saved = offer(monkeypatch, answers=[1])
    release = updater.newer_release("0.5.1", release_payload())
    controller._release = release
    controller._downloaded(release, tmp_path / "Maramax.app", None, "does not match its published SHA-256")
    assert "SHA-256" in alerts[0]["message"]
    assert item.title == "Install Maramax 0.5.2…"


def test_skipped_version_and_update_checks_persist(tmp_path):
    path = tmp_path / "settings.json"
    AppConfig(check_for_updates=False, skipped_update_version="0.5.2").save(path)
    loaded = AppConfig.load(path)
    assert loaded.check_for_updates is False and loaded.skipped_update_version == "0.5.2"
    assert AppConfig().check_for_updates is True
