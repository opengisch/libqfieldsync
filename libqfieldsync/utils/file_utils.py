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

import base64
import hashlib
import os
import platform
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple, Union

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsRasterMarkerSymbolLayer,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsSvgMarkerSymbolLayer,
    QgsSymbol,
    QgsVectorLayer,
)
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
        raise NoProjectFoundError(f"No .qgs file found in folder {path}") from None


def open_folder(path: Union[Path, str]) -> None:
    """
    Opens the provided path in a file browser.
    On Windows and Mac, this will open the parent directory
    and pre-select the actual folder.
    """
    path = Path(path)
    if platform.system() == "Windows":
        subprocess.Popen(rf'explorer /select,"{path}"')
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
            # TODO @suricactus: Python 3.9, pass `usedforsecurity=False`
            # https://app.clickup.com/t/2192114/QF-6481
            md5sum = hashlib.md5(file_data).hexdigest()  # noqa: S324

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
    """Check if the filename is valid."""
    pattern = re.compile(
        r'^(?!.*[<>:"/\\|?*])'
        r"(?!(?:COM[0-9]|CON|LPT[0-9]|NUL|PRN|AUX|com[0-9]|con|lpt[0-9]|nul|prn|aux)$)"
        r'[^\\\/:*"?<>|]{1,254}'
        r"(?<![\s\.])$"
    )

    return bool(pattern.match(filename))


def is_valid_filepath(path: str) -> bool:
    """Check if the entire path is valid by validating each part of the path."""
    path_obj = Path(path)
    for part in path_obj.parts:
        if not is_valid_filename(part):
            return False

    return True


def update_symbols_to_relative_embedded(
    symbol: QgsSymbol, home_path: Path, destination_path: Path
) -> None:
    """
    Update SVG or Raster symbols layer to relative path or embed it in the QGIS project.

    Args:
        symbol: The QGIS symbol (from a renderer).
        home_path: The root of QGIS Project home path.
        destination_path: The target directory where the exported project will be saved.

    """
    if symbol is None:
        return

    for symbol_layer in symbol.symbolLayers():
        # Filter out only symbology that includes SVG and Raster, skip the rest
        if not isinstance(
            symbol_layer, (QgsSvgMarkerSymbolLayer, QgsRasterMarkerSymbolLayer)
        ):
            continue

        source_path = Path(symbol_layer.path())

        # Check if symbol is already embedded
        if str(source_path)[:8].startswith("base64:"):
            continue

        # The symbol is already broken; its file is not reachable
        if not source_path.is_file():
            continue

        if source_path.is_relative_to(home_path):
            relative_path = source_path.relative_to(home_path)
            destination_path_file = destination_path.joinpath(relative_path)

            if destination_path_file.exists():
                symbol_layer.setPath(str(relative_path))
            else:
                encoded_data = base64.b64encode(source_path.read_bytes()).decode()
                symbol_layer.setPath(f"base64:{encoded_data}")

        else:
            encoded_data = base64.b64encode(source_path.read_bytes()).decode()
            symbol_layer.setPath(f"base64:{encoded_data}")


def set_relative_embed_layer_symbols_on_project(
    layer: QgsVectorLayer, project_home: Path, export_project_path: Path
) -> None:
    """
    Update the layer style SVG or Raster symbol paths to relative or embedded them in the QGIS project file.

    First try to ensure the paths are within to the QGIS project path.
    If the resulting path is impossible, then embed the symbols in the QGIS project.

    Args:
        layer: The QgsVectorLayer to update.  The layer is a point layer.
        project_home: The root of QGIS Project home path.
        export_project_path: The target directory for the exported offline QGIS project.

    """
    if Qgis.QGIS_VERSION_INT >= 33000:
        point_geometry = Qgis.GeometryType.Point
    else:
        from qgis.core import QgsWkbTypes

        point_geometry = QgsWkbTypes.GeometryType.PointGeometry

    if (
        not layer.isValid()
        or not isinstance(layer, QgsVectorLayer)
        or layer.geometryType() != point_geometry
    ):
        return

    renderer = layer.renderer()

    if not renderer:
        return

    if isinstance(renderer, QgsSingleSymbolRenderer):
        symbol = renderer.symbol()
        if symbol:
            update_symbols_to_relative_embedded(
                symbol, project_home, export_project_path
            )

    elif isinstance(renderer, QgsRuleBasedRenderer):
        for rule in renderer.rootRule().children():
            symbols = rule.symbols()

            if not symbols:
                continue

            for symbol in symbols:
                update_symbols_to_relative_embedded(
                    symbol, project_home, export_project_path
                )

    elif isinstance(renderer, QgsCategorizedSymbolRenderer):
        categories = renderer.categories()
        if categories:
            for index in range(len(categories)):
                # Get a fresh category.
                # The renderer doesn't update in-place modifications on categorized.
                category = renderer.categories()[index]
                symbol = category.symbol().clone()

                update_symbols_to_relative_embedded(
                    symbol, project_home, export_project_path
                )

                renderer.updateCategorySymbol(index, symbol)

    layer.setRenderer(renderer)
