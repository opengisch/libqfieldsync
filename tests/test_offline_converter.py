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
import unittest
from pathlib import Path

from qgis.core import QgsOfflineEditing, QgsProject
from qgis.testing import start_app
from qgis.testing.mocked import get_iface

from libqfieldsync.offline_converter import ExportType, OfflineConverter

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

    def test_cloud_packaging_pk(self):
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
            ExportType.Cloud,
        )
        offline_converter.convert()

        exported_project = self.load_project(
            self.target_dir.joinpath("project_qfield.qgs")
        )

        # spatialite layer
        layer = exported_project.mapLayersByName("somedata")[0]
        self.assertEqual(layer.customProperty("QFieldSync/sourceDataPrimaryKeys"), "pk")
        self.assertIsNone(layer.customProperty("QFieldSync/unsupported_source_pk"))
        self.assertFalse(layer.readOnly())

        # spatialite layer
        layer = exported_project.mapLayersByName("somepolydata")[0]
        self.assertEqual(layer.customProperty("QFieldSync/sourceDataPrimaryKeys"), "pk")
        self.assertIsNone(layer.customProperty("QFieldSync/unsupported_source_pk"))
        self.assertFalse(layer.readOnly())

        # gpkg layer
        layer = exported_project.mapLayersByName("curved_polys polys CurvePolygon")[0]
        self.assertEqual(
            layer.customProperty("QFieldSync/sourceDataPrimaryKeys"), "fid"
        )
        self.assertIsNone(layer.customProperty("QFieldSync/unsupported_source_pk"))
        self.assertFalse(layer.readOnly())

        # shp layer
        layer = exported_project.mapLayersByName("france_parts_shape")[0]
        self.assertIsNone(layer.customProperty("QFieldSync/sourceDataPrimaryKeys"))
        self.assertEqual(layer.customProperty("QFieldSync/unsupported_source_pk"), "1")
        self.assertTrue(layer.readOnly())
