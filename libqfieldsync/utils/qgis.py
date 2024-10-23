# -*- coding: utf-8 -*-

"""
/***************************************************************************
 QFieldSync
                              -------------------
        begin                : 2021
        copyright            : (C) 2021 by OPENGIS.ch
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

import os
import logging
import tempfile
from pathlib import Path
from typing import List, Optional, Union

from qgis.core import QgsMapLayer, QgsProject

logger = logging.getLogger(__name__)


def get_project_title(project: QgsProject) -> str:
    """Gets project title, or if non available, the basename of the filename"""
    if project.title():
        return project.title()
    else:
        return Path(project.fileName()).stem


def open_project(filename: str, filename_to_read: Optional[str] = None) -> bool:
    project = QgsProject.instance()
    project.clear()

    is_success = project.read(filename_to_read or filename)
    project.setFileName(filename)

    return is_success


def make_temp_qgis_file(
    project: QgsProject,
) -> str:
    project_backup_dir = tempfile.mkdtemp()
    original_filename = project.fileName()
    backup_filename = os.path.join(project_backup_dir, f"{project.baseName()}.qgs")
    project.write(backup_filename)
    project.setFileName(original_filename)

    return backup_filename


def get_memory_layers(project: QgsProject) -> List[QgsMapLayer]:
    return [
        layer
        for layer in project.mapLayers().values()
        if layer.isValid() and layer.dataProvider().name() == "memory"
    ]


def get_qgis_files_within_dir(dirname: Union[str, Path]) -> List[Path]:
    dirname = Path(dirname)
    return list(dirname.glob("*.qgs")) + list(dirname.glob("*.qgz"))
