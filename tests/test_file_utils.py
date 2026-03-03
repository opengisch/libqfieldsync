import hashlib
from pathlib import Path

import pytest
from qgis.testing import start_app

from libqfieldsync.utils.exceptions import NoProjectFoundError, QFieldSyncError
from libqfieldsync.utils.file_utils import (
    copy_additional_project_files,
    copy_attachments,
    copy_multifile,
    fileparts,
    get_children_with_extension,
    get_full_parent_path,
    get_project_in_folder,
    get_project_like_files,
    get_unique_empty_dirname,
    import_file_checksum,
    is_valid_filename,
    is_valid_filepath,
    isascii,
    open_folder,
    slugify,
)

start_app()


def test_fileparts_with_and_without_extension_dot():
    assert fileparts("/path/example/project.qgs") == (
        "/path/example",
        "project",
        ".qgs",
    )
    assert fileparts("/path/example/project.qgs", extension_dot=False) == (
        "/path/example",
        "project",
        "qgs",
    )


def test_get_children_with_extension_returns_expected_matches(tmp_path):
    parent = tmp_path.joinpath("parent")
    parent.mkdir()
    parent.joinpath("project.qgs").write_text("")
    parent.joinpath("other.txt").write_text("")

    assert get_children_with_extension(str(parent), "qgs") == [
        str(parent.joinpath("project.qgs"))
    ]


def test_get_children_with_extension_raises_for_missing_directory(tmp_path):
    with pytest.raises(QFieldSyncError):
        get_children_with_extension(str(tmp_path.joinpath("missing")), "qgs")


def test_get_children_with_extension_raises_for_unexpected_match_count(tmp_path):
    parent = tmp_path.joinpath("parent")
    parent.mkdir()
    parent.joinpath("a.qgs").write_text("")
    parent.joinpath("b.qgs").write_text("")

    with pytest.raises(QFieldSyncError):
        get_children_with_extension(str(parent), "qgs", count=1)


def test_get_full_parent_path_normalizes_parent(tmp_path):
    path = tmp_path.joinpath("nested", "..", "folder", "project.qgs")

    assert get_full_parent_path(str(path)) == str(tmp_path.joinpath("folder"))


def test_get_project_in_folder_returns_project(tmp_path):
    parent = tmp_path.joinpath("project")
    parent.mkdir()
    project_file = parent.joinpath("project.qgs")
    project_file.write_text("")

    assert get_project_in_folder(str(parent)) == str(project_file)


def test_get_project_in_folder_raises_when_missing(tmp_path):
    parent = tmp_path.joinpath("project")
    parent.mkdir()

    with pytest.raises(NoProjectFoundError):
        get_project_in_folder(str(parent))


def test_open_folder_uses_windows_explorer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "libqfieldsync.utils.file_utils.platform.system", lambda: "Windows"
    )
    monkeypatch.setattr("libqfieldsync.utils.file_utils.subprocess.Popen", calls.append)

    path = tmp_path.joinpath("folder")
    open_folder(path)

    assert calls == [rf'explorer /select,"{path}"']


def test_open_folder_uses_macos_open(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "libqfieldsync.utils.file_utils.platform.system", lambda: "Darwin"
    )
    monkeypatch.setattr("libqfieldsync.utils.file_utils.subprocess.Popen", calls.append)

    path = tmp_path.joinpath("folder")
    open_folder(path)

    assert calls == [["open", "-R", path]]


def test_open_folder_uses_xdg_open(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "libqfieldsync.utils.file_utils.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr("libqfieldsync.utils.file_utils.subprocess.Popen", calls.append)

    path = tmp_path.joinpath("folder")
    open_folder(path)

    assert calls == [["xdg-open", path]]


def test_open_folder_uses_xdg_open_for_unknown_os(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "libqfieldsync.utils.file_utils.platform.system", lambda: "FreeBSD"
    )
    monkeypatch.setattr("libqfieldsync.utils.file_utils.subprocess.Popen", calls.append)

    path = tmp_path.joinpath("folder")
    open_folder(path)

    assert calls == [["xdg-open", path]]


def test_import_file_checksum_prefers_gpkg_and_falls_back_to_sqlite(tmp_path):
    gpkg_dir = tmp_path.joinpath("gpkg")
    gpkg_dir.mkdir()
    gpkg_bytes = b"gpkg data"
    gpkg_dir.joinpath("data.gpkg").write_bytes(gpkg_bytes)

    sqlite_dir = tmp_path.joinpath("sqlite")
    sqlite_dir.mkdir()
    sqlite_bytes = b"sqlite data"
    sqlite_dir.joinpath("data.sqlite").write_bytes(sqlite_bytes)

    assert (
        import_file_checksum(str(gpkg_dir))
        == hashlib.md5(gpkg_bytes, usedforsecurity=False).hexdigest()
    )
    assert (
        import_file_checksum(str(sqlite_dir))
        == hashlib.md5(sqlite_bytes, usedforsecurity=False).hexdigest()
    )
    assert import_file_checksum(str(tmp_path.joinpath("missing"))) is None


def test_slugify_normalizes_unicode_and_separators():
    assert slugify("Café déjà vu / Layer 01") == "cafe-de-ja-vu-layer-01"


def test_copy_attachments_copies_nested_tree(tmp_path):
    source_root = tmp_path.joinpath("source")
    dest_root = tmp_path.joinpath("dest")
    attachments_dir = source_root.joinpath("DCIM", "subfolder")
    attachments_dir.mkdir(parents=True)
    source_root.joinpath("DCIM", "photo.jpg").write_text("image")
    attachments_dir.joinpath("nested.jpg").write_text("nested")

    copy_attachments(source_root, dest_root, Path("DCIM"))

    assert (
        dest_root.joinpath("DCIM", "photo.jpg").read_text(encoding="utf-8") == "image"
    )
    assert (
        dest_root.joinpath("DCIM", "subfolder", "nested.jpg").read_text(
            encoding="utf-8"
        )
        == "nested"
    )


def test_copy_multifile_copies_gpkg_sidecars(tmp_path):
    source = tmp_path.joinpath("source.gpkg")
    dest = tmp_path.joinpath("dest.gpkg")
    source.write_text("main")
    tmp_path.joinpath("source.gpkg-shm").write_text("shm")
    tmp_path.joinpath("source.gpkg-wal").write_text("wal")

    copy_multifile(source, dest)

    assert dest.read_text(encoding="utf-8") == "main"
    assert tmp_path.joinpath("dest.gpkg-shm").read_text(encoding="utf-8") == "shm"
    assert tmp_path.joinpath("dest.gpkg-wal").read_text(encoding="utf-8") == "wal"


def test_get_unique_empty_dirname_reuses_empty_and_increments_non_empty(tmp_path):
    base = tmp_path.joinpath("export")
    assert get_unique_empty_dirname(base) == base

    base.mkdir()
    assert get_unique_empty_dirname(base) == base

    base.joinpath("file.txt").write_text("data")
    base_1 = tmp_path.joinpath("export_1")
    base_1.mkdir()
    base_1.joinpath("file.txt").write_text("data")

    assert get_unique_empty_dirname(base) == tmp_path.joinpath("export_2")


def test_isascii_and_path_validation_helpers():
    assert isascii("plain-file.txt") is True
    assert isascii("ümlaut.txt") is False
    assert isascii("ки/ри/ли/ца.txt") is False
    assert is_valid_filename("valid-name_01.txt") is True
    assert is_valid_filename("bad:name.txt") is False
    assert is_valid_filename("CON") is False
    assert is_valid_filepath("folder/subfolder/file.txt") is True
    assert is_valid_filepath("folder/bad:name.txt") is False


def test_get_project_like_files_returns_matching_sidecars(tmp_path):
    project = tmp_path.joinpath("my.project.qgs")
    project.write_text("")
    qml_file = tmp_path.joinpath("my.project.qml")
    qml_file.write_text("")
    qm_file = tmp_path.joinpath("my.project_de.qm")
    qm_file.write_text("")
    tmp_path.joinpath("otherproject.qml").write_text("")

    assert sorted(get_project_like_files(project, ".*")) == sorted(
        [str(project), str(qml_file)]
    )
    assert get_project_like_files(project, "_??.qm") == [str(qm_file)]


def test_copy_additional_project_files_renames_qml_and_qm_files(tmp_path):
    source_dir = tmp_path.joinpath("source")
    export_dir = tmp_path.joinpath("export")
    source_dir.mkdir()
    export_dir.mkdir()

    source_project = source_dir.joinpath("project.qgs")
    export_project = export_dir.joinpath("project_qfield.qgs")
    source_project.write_text("")
    export_project.write_text("")

    plugin_file = source_dir.joinpath("project.qml")
    translation_file = source_dir.joinpath("project_de.qm")
    nested_translation_file = source_dir.joinpath("i18n", "project_fr.qm")
    asset_file = source_dir.joinpath("notes.txt")

    nested_translation_file.parent.mkdir()
    plugin_file.write_text("plugin")
    translation_file.write_text("de")
    nested_translation_file.write_text("fr")
    asset_file.write_text("notes")

    copy_additional_project_files(
        source_project,
        export_project,
        [
            str(plugin_file),
            str(translation_file),
            str(nested_translation_file),
            str(asset_file),
        ],
    )

    assert (
        export_dir.joinpath("project_qfield.qml").read_text(encoding="utf-8")
        == "plugin"
    )
    assert (
        export_dir.joinpath("project_qfield_de.qm").read_text(encoding="utf-8") == "de"
    )
    assert (
        export_dir.joinpath("i18n", "project_qfield_fr.qm").read_text(encoding="utf-8")
        == "fr"
    )
    assert export_dir.joinpath("notes.txt").read_text(encoding="utf-8") == "notes"
