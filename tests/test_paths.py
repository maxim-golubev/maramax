from pathlib import Path

from parakeet_dictation.paths import resource_path


def test_resource_path_falls_back_to_repo_assets():
    path = resource_path("assets", "menu_icon.png")

    assert path == Path(__file__).resolve().parents[1] / "assets" / "menu_icon.png"
