"""The release scripts' own rules, on folders made in tmp_path: no build, no signing, no network."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "packaging"))
    import create_release
    import publish_release
    return create_release, publish_release


def release_folder(root, version, *, signing="Maramax release certificate", published=True):
    folder = root / "releases" / f"Maramax-{version}"
    (folder / "Maramax.app").mkdir(parents=True)
    (folder / "build-info.json").write_text(json.dumps({"version": version, "signing": signing}))
    if published:
        (root / "releases" / f"Maramax-{version}.published").write_text(f"v{version} abc\n")
    return folder


def test_a_delta_starts_from_the_newest_earlier_published_release(tmp_path, scripts):
    create_release, _ = scripts
    release_folder(tmp_path, "0.6.2")
    release_folder(tmp_path, "0.6.3")
    release_folder(tmp_path, "0.6.4", published=False)       # Built, then abandoned: no copy is at it.
    release_folder(tmp_path, "0.6.5", signing="ad hoc")
    release_folder(tmp_path, "0.7.1")
    assert create_release.previous_release(tmp_path, "0.7.0") == (
        "0.6.3", tmp_path / "releases" / "Maramax-0.6.3" / "Maramax.app")
    assert create_release.previous_release(tmp_path, "0.6.2") is None


def test_an_unreadable_earlier_release_stops_the_release_with_its_name(tmp_path, scripts):
    create_release, _ = scripts
    folder = release_folder(tmp_path, "0.6.3")
    (folder / "build-info.json").write_text("{not json")
    with pytest.raises(create_release.ReleaseError, match="Maramax-0.6.3"):
        create_release.previous_release(tmp_path, "0.7.0")


def stamp(tmp_path, content):
    path = tmp_path / "build-stamp.json"
    path.write_text(content)
    return path


def test_the_build_records_its_own_commit_and_dirty_flag(tmp_path, scripts):
    create_release, _ = scripts
    assert create_release.built_dirty(stamp(tmp_path, '{"commit": "abc", "dirty": false}\n'), "abc") is False
    assert create_release.built_dirty(stamp(tmp_path, '{"commit": "abc", "dirty": true}\n'), "abc") is True


@pytest.mark.parametrize("content, message", [
    ('{"commit": "old", "dirty": false}', "built from old, but HEAD is abc"),   # Committed after the build.
    ('{"commit": "abc", "dirty": "no"}', "a commit and a dirty flag"),
    ("{", "cannot be read"),
])
def test_a_bundle_not_built_from_head_is_refused(tmp_path, scripts, content, message):
    create_release, _ = scripts
    with pytest.raises(create_release.ReleaseError, match=message):
        create_release.built_dirty(stamp(tmp_path, content), "abc")


def test_a_missing_build_stamp_asks_for_a_build(tmp_path, scripts):
    create_release, _ = scripts
    with pytest.raises(create_release.ReleaseError, match="build_app.sh"):
        create_release.built_dirty(tmp_path / "build-stamp.json", "abc")


def test_a_relative_notes_file_is_the_one_in_the_directory_it_was_named_from(tmp_path, scripts, monkeypatch):
    _, publish_release = scripts
    monkeypatch.chdir(tmp_path)
    assert publish_release.arguments(["--notes-file", "notes.md"]).notes_file == tmp_path.resolve() / "notes.md"
