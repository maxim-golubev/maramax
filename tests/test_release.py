"""The release scripts' own rules, on folders and git repositories made in tmp_path: no build, no signing, no network."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

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


def test_the_way_to_release_again_names_everything_an_earlier_run_left(tmp_path, scripts):
    create_release, _ = scripts
    steps = create_release.steps_to_release_again(tmp_path, "0.9.0")
    for left in (str(tmp_path / "releases" / "Maramax-0.9.0"), "Maramax-0.9.0.zip", "Maramax-0.9.0-from-*.delta",
                 "Maramax-0.9.0.assets.json", ".sha256"):
        assert left in steps
    assert steps.endswith("then run create_release.py")


@pytest.fixture
def git(monkeypatch):
    """Runs git as on a machine with no git configuration of its own (no hooks, signing, or filters)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Release Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "release@example.invalid")

    def run(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    return run


def checkout_with_origin(tmp_path, git):
    origin, checkout = tmp_path / "origin.git", tmp_path / "checkout"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "init", "-q", str(checkout))
    git(checkout, "remote", "add", "origin", str(origin))
    git(checkout, "commit", "-q", "--allow-empty", "-m", "Release")
    return origin, checkout


def test_a_tag_on_origin_is_found_and_an_absent_one_is_not(tmp_path, scripts, git):
    _, publish_release = scripts
    _, checkout = checkout_with_origin(tmp_path, git)
    git(checkout, "tag", "v0.9.0")
    git(checkout, "push", "-q", "origin", "v0.9.0")
    assert publish_release.origin_has_tag(checkout, "v0.9.0") is True
    assert publish_release.origin_has_tag(checkout, "v0.9.1") is False


def test_an_origin_git_cannot_reach_stops_the_publish_instead_of_reading_as_no_tag(tmp_path, scripts, git):
    _, publish_release = scripts
    origin, checkout = checkout_with_origin(tmp_path, git)
    shutil.rmtree(origin)  # As unreachable as a network outage or a refused credential: git exits 128.
    with pytest.raises(publish_release.PublishError, match=r"whether v0\.9\.0 exists \(git ls-remote exit 128\)"):
        publish_release.origin_has_tag(checkout, "v0.9.0")


def test_the_committed_sources_are_hashed_as_create_release_hashes_the_bundled_ones(tmp_path, scripts, git):
    _, publish_release = scripts
    _, checkout = checkout_with_origin(tmp_path, git)
    sources = checkout / "src" / "parakeet_dictation"
    (sources / "helpers.py").mkdir(parents=True)  # A folder, not a source, whatever its name.
    (sources / "helpers.py" / "inner.py").write_text("inner = 1\n")
    (sources / "__init__.py").write_text('__version__ = "0.9.0"\n')
    (sources / "app.py").write_text("print('dictate')\n")
    (sources / "README.txt").write_text("not code\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "Sources")
    (sources / "app.py").write_text("print('edited after the commit')\n")  # Only the commit counts.
    assert publish_release.committed_sources(checkout, git(checkout, "rev-parse", "HEAD")) == {
        "src/parakeet_dictation/__init__.py": hashlib.sha256(b'__version__ = "0.9.0"\n').hexdigest(),
        "src/parakeet_dictation/app.py": hashlib.sha256(b"print('dictate')\n").hexdigest(),
    }


def test_a_bundled_source_no_commit_holds_is_named(scripts):
    _, publish_release = scripts
    committed = {"src/a.py": "1", "src/b.py": "2", "src/c.py": "3"}
    assert publish_release.differing_sources(dict(committed), committed) == []
    assert publish_release.differing_sources(
        {"src/a.py": "1", "src/b.py": "edited during the build", "src/d.py": "4"}, committed,
    ) == ["src/b.py", "src/c.py", "src/d.py"]


def test_the_bundle_check_builds_every_window_of_this_source():
    """check_bundle.py runs inside a built app; a window interface it still
    calls the old way would otherwise fail only the next build."""
    script = (f"import sys; sys.path.insert(0, {str(Path(__file__).parents[1] / 'packaging')!r}); "
              "import check_bundle; check_bundle.check_windows(); print('windows ok')")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "windows ok" in result.stdout, result.stderr[-2000:]
