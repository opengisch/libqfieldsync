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

from qgis.core import QgsEditorWidgetSetup, QgsProject, QgsVectorLayer
from qgis.testing import start_app
from qgis.testing.mocked import get_iface

from libqfieldsync.layer import LayerSource

# Cleaning up the application is turned off to avoid segfaults upon exiting tests
start_app(cleanup=False)


class LayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iface = get_iface()

    def setUp(self):
        self.source_dir = Path(tempfile.mkdtemp())
        self.target_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.source_dir)
        shutil.rmtree(self.target_dir)
        QgsProject.instance().clear()

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

    def test_attachment_accepted_types(self):
        vector_layer = QgsVectorLayer(
            "Point?field=the_photo_field:string&field=other:string",
            "attachments",
            "memory",
        )
        vector_layer.setEditorWidgetSetup(
            0, QgsEditorWidgetSetup("ExternalResource", {"DocumentViewer": 1})
        )

        layer_source = LayerSource(vector_layer)
        self.assertEqual(layer_source.attachment_accepted_types("the_photo_field"), [])

        layer_source.set_attachment_accepted_types(
            "the_photo_field",
            [
                LayerSource.AttachmentAcceptedType.IMAGE,
                LayerSource.AttachmentAcceptedType.AUDIO,
            ],
        )
        layer_source.apply()
        self.assertEqual(
            vector_layer.customProperty("QFieldSync/attachment_accepted_types"),
            '{"the_photo_field": ["image", "audio"]}',
        )

        layer_source = LayerSource(vector_layer)
        self.assertEqual(
            layer_source.attachment_accepted_types("the_photo_field"),
            [
                LayerSource.AttachmentAcceptedType.IMAGE,
                LayerSource.AttachmentAcceptedType.AUDIO,
            ],
        )
        self.assertEqual(layer_source.attachment_accepted_types("other"), [])

        vector_layer.setCustomProperty(
            "QFieldSync/attachment_accepted_types",
            '{"the_photo_field": ["video", "unknown"]}',
        )
        layer_source = LayerSource(vector_layer)
        self.assertEqual(
            layer_source.attachment_accepted_types("the_photo_field"),
            [LayerSource.AttachmentAcceptedType.VIDEO],
        )


if __name__ == "__main__":
    unittest.main()
