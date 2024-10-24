# -*- coding: utf-8 -*-

"""
/***************************************************************************
 QFieldSync
                              -------------------
        begin                : 2016
        copyright            : (C) 2016 by OPENGIS.ch
        email                : info@opengis.ch
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import hashlib
import os
import platform
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple, Union

from qgis.PyQt.QtCore import QCoreApplication

from .exceptions import NoProjectFoundError, QFieldSyncError


def fileparts(filename: str, extension_dot: bool = True) -> Tuple[str, str, str]:
    path = os.path.dirname(filename)
    basename = os.path.basename(filename)
    name, ext = os.path.splitext(basename)
    if extension_dot and not ext.startswith(".") and ext:
        ext = "." + ext
    elif not extension_dot and ext.startswith("."):
        ext = ext[1:]
    return (path, name, ext)


def get_children_with_extension(
    parent: str, specified_ext: str, count: int = 1
) -> List[str]:
    if not os.path.isdir(parent):
        raise QFieldSyncError(
            QCoreApplication.translate(
                "QFieldFileUtils", "The directory {} could not be found"
            ).format(parent)
        )

    res = []
    extension_dot = specified_ext.startswith(".")
    for filename in os.listdir(parent):
        _, _, ext = fileparts(filename, extension_dot=extension_dot)
        if ext == specified_ext:
            res.append(os.path.join(parent, filename))
    if len(res) != count:
        raise QFieldSyncError(
            QCoreApplication.translate(
                "QFieldFileUtils",
                "Expected {expected_count} children with extension {file_extension} under {parent}, got {real_count}",
            ).format(
                expected_count=count,
                file_extension=specified_ext,
                parent=parent,
                real_count=len(res),
            )
        )

    return res


def get_full_parent_path(filename: str) -> str:
    return os.path.dirname(os.path.normpath(filename))


def get_project_in_folder(path: str) -> str:
    try:
        return get_children_with_extension(path, "qgs", count=1)[0]
    except QFieldSyncError:
        message = "No .qgs file found in folder {}".format(path)
        raise NoProjectFoundError(message)


def open_folder(path: Union[Path, str]) -> None:
    """
    Opens the provided path in a file browser.
    On Windows and Mac, this will open the parent directory
    and pre-select the actual folder.
    """
    path = Path(path)
    if platform.system() == "Windows":
        subprocess.Popen(r'explorer /select,"{}"'.format(path))
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", path])


def import_file_checksum(path: str) -> Optional[str]:
    md5sum = None
    path = os.path.join(path, "data.gpkg")
    if not os.path.exists(path):
        path = os.path.join(path, "data.sqlite")
    if os.path.exists(path):
        with open(path, "rb") as f:
            file_data = f.read()
            md5sum = hashlib.md5(file_data).hexdigest()

    return md5sum


def slugify(text: str) -> str:
    # https://stackoverflow.com/q/5574042/1548052
    slug = unicodedata.normalize("NFKD", text)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", slug).strip("-")
    slug = re.sub(r"[-]+", "-", slug)
    slug = slug.lower()
    return slug


def copy_attachments(source: Path, dest: Path, dirname: Path) -> None:
    source = source.joinpath(dirname)
    dest = dest.joinpath(dirname)

    if source.is_dir():
        dest.mkdir(parents=True, exist_ok=True)

    for filename in source.glob("**/*"):
        relative_filename = filename.relative_to(source)
        dest_filename = dest.joinpath(relative_filename)

        # create the folder
        if filename.is_dir():
            dest_filename.mkdir(parents=True, exist_ok=True)
            copy_attachments(source, dest, relative_filename)
            continue

        # copy the file no matter if it exists or not
        shutil.copyfile(filename, dest_filename)


def copy_multifile(
    source_filename: Union[str, Path], dest_filename: Union[str, Path]
) -> None:
    """Copies a file from source to destination. If the file is GPKG, also copies the "-wal" and "-shm" files"""
    source = str(source_filename)
    dest = str(dest_filename)

    if source.endswith(".gpkg"):
        for suffix in ("-shm", "-wal"):
            source_path = source + suffix
            dest_path = dest + suffix

            if Path(source_path).exists():
                shutil.copyfile(source_path, dest_path)

    shutil.copyfile(source, str(dest_filename))


def get_unique_empty_dirname(dirname: Union[str, Path]) -> Path:
    dirname = Path(dirname)

    if not dirname.exists() or len(list(dirname.iterdir())) == 0:
        return dirname

    i = 1
    while True:
        new_dirname = Path(f"{dirname}_{i}")

        if not new_dirname.exists() or len(list(new_dirname.iterdir())) == 0:
            return new_dirname

        i += 1


def isascii(filename: str) -> bool:
    try:
        return filename.isascii()
    except Exception:
        try:
            filename.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False


def is_valid_filename(filename: str) -> bool:
    """
    Check if the filename is valid.
    """
    pattern = re.compile(
        r'^(?!.*[<>:"/\\|?*])'
        r"(?!(?:COM[0-9]|CON|LPT[0-9]|NUL|PRN|AUX|com[0-9]|con|lpt[0-9]|nul|prn|aux)$)"
        r'[^\\\/:*"?<>|]{1,254}'
        r"(?<![\s\.])$"
    )

    return bool(pattern.match(filename))


def is_valid_filepath(path: str) -> bool:
    """
    Check if the entire path is valid by validating each part of the path.
    """
    path_obj = Path(path)
    for part in path_obj.parts:
        if not is_valid_filename(part):
            return False

    return True
