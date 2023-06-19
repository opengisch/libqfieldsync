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

import shutil
import tempfile
from pathlib import Path

from qgis.core import QgsOfflineEditing, QgsProject
from qgis.testing import start_app, unittest
from qgis.testing.mocked import get_iface

from ..offline_converter import OfflineConverter

start_app()


class OfflineConverterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iface = get_iface()

    def setUp(self):
        QgsProject.instance().clear()
        self.source_dir = Path(tempfile.mkdtemp())
        self.target_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.source_dir)
        shutil.rmtree(self.target_dir)

    @property
    def data_dir(self) -> Path:
        return Path(__file__).parent.joinpath("data")

    def load_project(self, path):
        project = QgsProject.instance()
        self.assertTrue(project.read(str(path)))
        return project

    def test_copy(self):
        shutil.copytree(
            self.data_dir.joinpath("simple_project"),
            self.source_dir.joinpath("simple_project"),
        )

        project = self.load_project(
            self.source_dir.joinpath("simple_project", "project.qgs")
        )
        offline_editing = QgsOfflineEditing()
        offline_converter = OfflineConverter(
            project,
            str(self.target_dir),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            offline_editing,
        )
        offline_converter.convert()

        self.assertTrue(self.target_dir.joinpath("project_qfield.qgs").exists())
        self.assertTrue(self.target_dir.joinpath("france_parts_shape.shp").exists())
        self.assertTrue(self.target_dir.joinpath("france_parts_shape.dbf").exists())
        self.assertTrue(self.target_dir.joinpath("curved_polys.gpkg").exists())
        self.assertTrue(self.target_dir.joinpath("spatialite.db").exists())
        self.assertTrue(self.target_dir.joinpath("DCIM", "qfield-photo_1.jpg").exists())
        self.assertTrue(self.target_dir.joinpath("DCIM", "qfield-photo_2.jpg").exists())
        self.assertTrue(self.target_dir.joinpath("DCIM", "qfield-photo_3.jpg").exists())
        self.assertTrue(
            self.target_dir.joinpath(
                "DCIM", "subfolder", "qfield-photo_sub_1.jpg"
            ).exists()
        )
        self.assertTrue(
            self.target_dir.joinpath(
                "DCIM", "subfolder", "qfield-photo_sub_2.jpg"
            ).exists()
        )
        self.assertTrue(
            self.target_dir.joinpath(
                "DCIM", "subfolder", "qfield-photo_sub_3.jpg"
            ).exists()
        )

    def test_primary_keys_custom_property(self):
        shutil.copytree(
            self.data_dir.joinpath("simple_project"),
            self.source_dir.joinpath("simple_project"),
        )

        project = self.load_project(
            self.source_dir.joinpath("simple_project", "project.qgs")
        )
        offline_editing = QgsOfflineEditing()
        offline_converter = OfflineConverter(
            project,
            str(self.target_dir),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            offline_editing,
        )
        offline_converter.convert()

        exported_project = self.load_project(
            self.target_dir.joinpath("project_qfield.qgs")
        )
        layer = exported_project.mapLayersByName("somedata")[0]
        self.assertEqual(layer.customProperty("QFieldSync/sourceDataPrimaryKeys"), "pk")
