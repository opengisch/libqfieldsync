# -*- coding: utf-8 -*-

"""
/***************************************************************************
 QFieldSync
                              -------------------
        begin                : 2024.08.21
        copyright            : (C) 2024 by Mathieu Pellerin
        email                : mathieu@opengis.ch
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

from qgis.core import QgsProject, QgsVectorLayer
from qgis.testing import start_app
from qgis.testing.mocked import get_iface

from libqfieldsync.layer import LayerSource

start_app()


class LayerTest(unittest.TestCase):
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

    def test_conver_to_gpkg(self):
        shutil.copytree(
            self.data_dir.joinpath("simple_project"),
            self.source_dir.joinpath("simple_project"),
        )

        vector_layer = QgsVectorLayer(
            str(self.source_dir.joinpath("simple_project/france_parts_shape.shp")),
            "france_parts",
        )
        source_feature_count = vector_layer.featureCount()

        layer_source = LayerSource(vector_layer)
        target_file = layer_source.convert_to_gpkg(str(self.target_dir))
        target_feature_count = layer_source.layer.featureCount()

        self.assertTrue(self.target_dir.joinpath("franceparts.gpkg").exists())
        self.assertEqual(str(self.target_dir.joinpath("franceparts.gpkg")), target_file)
        self.assertEqual(source_feature_count, target_feature_count)
