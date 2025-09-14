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

from qgis.core import QgsProject
from qgis.testing import start_app
from qgis.testing.mocked import get_iface

from libqfieldsync.layer import LayerSource
from libqfieldsync.offline_converter import ExportType, OfflineConverter
from libqfieldsync.offliners import PythonMiniOffliner
from libqfieldsync.utils.bad_layer_handler import (
    bad_layer_handler,
    set_bad_layer_handler,
)

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
        return
        shutil.copytree(
            self.data_dir.joinpath("simple_project"),
            self.source_dir.joinpath("simple_project"),
        )

        project = self.load_project(
            self.source_dir.joinpath("simple_project", "project.qgs")
        )
        offline_converter = OfflineConverter(
            project,
            self.target_dir.joinpath("project_qfield.qgs"),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            PythonMiniOffliner(),
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

    def test_assets(self):
        shutil.copytree(
            self.data_dir.joinpath("assets_project"),
            self.source_dir.joinpath("assets_project"),
        )

        project = self.load_project(
            self.source_dir.joinpath("assets_project", "project.qgs")
        )
        offline_converter = OfflineConverter(
            project,
            self.target_dir.joinpath("project_qfield.qgs"),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            PythonMiniOffliner(),
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
        self.assertTrue(
            self.target_dir.joinpath("assets", "qfield_for_qgis.png").exists()
        )
        self.assertTrue(self.target_dir.joinpath("project_qfield.qml").exists())
        self.assertTrue(self.target_dir.joinpath("project_qfield_fr.qm").exists())
        self.assertTrue(self.target_dir.joinpath("project_qfield_de.qm").exists())

    def test_primary_keys_custom_property(self):
        shutil.copytree(
            self.data_dir.joinpath("simple_project"),
            self.source_dir.joinpath("simple_project"),
        )

        project = self.load_project(
            self.source_dir.joinpath("simple_project", "project.qgs")
        )
        offline_converter = OfflineConverter(
            project,
            self.target_dir.joinpath("project_qfield.qgs"),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            PythonMiniOffliner(),
        )
        offline_converter.convert()

        exported_project = self.load_project(
            self.target_dir.joinpath("project_qfield.qgs")
        )
        layer = exported_project.mapLayersByName("somedata")[0]
        self.assertEqual(layer.customProperty("QFieldSync/sourceDataPrimaryKeys"), "pk")

    def test_geometryless_layers(self):
        shutil.copytree(
            self.data_dir.joinpath("geometryless_project"),
            self.source_dir.joinpath("geometryless_project"),
        )

        project = self.load_project(
            self.source_dir.joinpath("geometryless_project", "project.qgs")
        )
        offline_converter = OfflineConverter(
            project,
            self.target_dir.joinpath("project_qfield.qgs"),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            PythonMiniOffliner(),
        )
        offline_converter.convert()

        exported_project = self.load_project(
            self.target_dir.joinpath("project_qfield.qgs")
        )
        layer = exported_project.mapLayersByName("csv")[0]
        self.assertEqual(layer.featureCount(), 2)
        layer = exported_project.mapLayersByName("ods")[0]
        self.assertEqual(layer.featureCount(), 2)
        layer = exported_project.mapLayersByName("xlsx")[0]
        self.assertEqual(layer.featureCount(), 2)

    def test_localized_layers(self):
        shutil.copytree(
            self.data_dir.joinpath("localized_project"),
            self.source_dir.joinpath("localized_project"),
        )

        project = QgsProject.instance()
        project.setBadLayerHandler(bad_layer_handler)
        with set_bad_layer_handler(project):
            project = self.load_project(
                self.source_dir.joinpath("localized_project", "project.qgs")
            )
            localized_layer1 = project.mapLayers().get(
                "Giveaways2021_838ae916_af46_4ca3_80d6_fd89c57c3e6f", None
            )

            self.assertIsNotNone(localized_layer1)
            self.assertTrue(LayerSource(localized_layer1).is_localized_path)

            localized_layer2 = project.mapLayers().get(
                "2024_03_30_00_00_2024_03_30_23_59_Sentinel_2_L2A_True_color3_f174e0e3_a294_4ebb_81b8_ec4efd9ff5f9",
                None,
            )

            self.assertIsNotNone(localized_layer2)
            self.assertTrue(LayerSource(localized_layer2).is_localized_path)

            non_localized_layer1 = project.mapLayers().get(
                "geometryless_25cfdaaa_8a2c_4b98_94ed_4a760c8ead6c", None
            )

            self.assertIsNotNone(non_localized_layer1)
            self.assertFalse(LayerSource(non_localized_layer1).is_localized_path)

        offline_converter = OfflineConverter(
            project,
            self.target_dir.joinpath("project_qfield.qgs"),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            PythonMiniOffliner(),
        )
        offline_converter.convert()

        exported_project = self.load_project(
            self.target_dir.joinpath("project_qfield.qgs")
        )
        self.assertEqual(len(exported_project.mapLayers()), 3)

    def test_cloud_packaging_pk(self):
        shutil.copytree(
            self.data_dir.joinpath("simple_project"),
            self.source_dir.joinpath("simple_project"),
        )

        project = self.load_project(
            self.source_dir.joinpath("simple_project", "project.qgs")
        )
        offline_converter = OfflineConverter(
            project,
            self.target_dir.joinpath("project_qfield.qgs"),
            "POLYGON((1 1, 5 0, 5 5, 0 5, 1 1))",
            QgsProject.instance().crs().authid(),
            ["DCIM"],
            PythonMiniOffliner(),
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
