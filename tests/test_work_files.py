from pathlib import Path

import pytest

from agent_core.file_work import FileWorkService


def test_list_files_sorts_and_ignores_directories_and_links(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "folder").mkdir()
    result = FileWorkService([tmp_path]).list_files(tmp_path, sort_by="name", descending=False)
    assert [item["name"] for item in result["items"]] == ["a.py", "b.txt"]


def test_preview_moves_by_extension_requires_allowed_root_and_skips_collisions(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "txt").mkdir()
    (tmp_path / "txt" / "a.txt").write_text("existing")
    service = FileWorkService([tmp_path])
    preview = service.preview_sort_by_extension(tmp_path)
    assert preview["moves"] == []
    assert preview["skipped"][0]["reason"] == "destination_exists"


def test_preview_rejects_path_outside_allowed_root(tmp_path: Path) -> None:
    service = FileWorkService([tmp_path / "allowed"])
    with pytest.raises(PermissionError):
        service.list_files(tmp_path, sort_by="name")


def test_apply_moves_validates_snapshot_before_each_move(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("a")
    service = FileWorkService([tmp_path])
    preview = service.preview_sort_by_extension(tmp_path)
    source.unlink()
    result = service.apply_moves(preview["moves"])
    assert result["status"] == "stale"
    assert not (tmp_path / "txt" / "a.txt").exists()


def test_list_directories_returns_roots_when_no_directory_given(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    result = FileWorkService([root]).list_directories()
    assert result["directory"] == ""
    assert result["parent"] is None
    assert result["items"] == [{"name": "allowed", "path": str(root)}]


def test_list_directories_lists_only_subdirs_with_parent_inside_root(tmp_path: Path) -> None:
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "c.txt").write_text("c")
    result = FileWorkService([tmp_path]).list_directories(tmp_path)
    assert [item["name"] for item in result["items"]] == ["a_dir", "b_dir"]
    assert result["directory"] == str(tmp_path)
    assert result["parent"] is None  # 根目录本身没有可回退的上级


def test_list_directories_parent_stops_at_allowed_root(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    inner.mkdir()
    result = FileWorkService([tmp_path]).list_directories(inner)
    assert result["parent"] == str(tmp_path)


def test_list_directories_rejects_path_outside_allowed_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    service = FileWorkService([allowed])
    with pytest.raises(PermissionError):
        service.list_directories(outside)


def test_list_directories_rejects_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("a")
    with pytest.raises(NotADirectoryError):
        FileWorkService([tmp_path]).list_directories(target)
