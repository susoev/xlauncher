from pathlib import Path

from xlauncher.winshell import points_into_app


def test_points_into_app(tmp_path: Path):
    root = tmp_path / "xl"
    assert points_into_app(str(root / "app" / "2026.11.26.5" / "python" / "XLauncher.exe"), root)
    assert not points_into_app(str(root / "xlauncher.exe"), root)
    assert not points_into_app(str(root / "application.exe"), root)  # не путать с app\
    assert not points_into_app("", root)
