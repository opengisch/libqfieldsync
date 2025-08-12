"""
/***************************************************************************
 QFieldSync
                              -------------------
        begin                : 2323
        copyright            : (C) 2323 by OPENGIS.ch
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

from typing import List

from qgis.core import QgsProject, QgsProjectBadLayerHandler
from qgis.PyQt.QtXml import QDomNode


class BadLayerHandler(QgsProjectBadLayerHandler):
    invalid_layer_sources_by_id = {}

    def handleBadLayers(self, layer_nodes: List[QDomNode]):
        super().handleBadLayers(layer_nodes)

        for layer_node in layer_nodes:
            layer_id = layer_node.namedItem("id").toElement().text()
            self.invalid_layer_sources_by_id[layer_id] = self.dataSource(layer_node)

    def clear(self):
        """Clears the invalid layers dictionary"""
        self.invalid_layer_sources_by_id.clear()


# poor man singleton. Metaclass does not work because `QgsProjectBadLayerHandler` does not have the same metaclass. Other singleton options are not "good" enough.
bad_layer_handler = BadLayerHandler()


class set_bad_layer_handler:
    """QGIS bad layer handler catches all unavailable layers, including the localized ones.
    Can be used a context manager or decorator around `QgsProject.read()` call.
    """

    def __init__(self, project: QgsProject):
        self.project = project

    def __enter__(self):
        bad_layer_handler.clear()
        # NOTE we should set the bad layer handler only when we need it.
        # Unfortunately we cannot due to a crash when calling `QgsProject.read()` when we already used this context manager.
        # The code below is used as documentation for future generations of engineers willing to fix this.
        # self.project.setBadLayerHandler(bad_layer_handler)

    def __exit__(self, exc_type, exc_value, traceback):
        # NOTE we should set the bad layer handler only when we need it.
        # Unfortunately we cannot due to a crash when calling `QgsProject.read()` when we already used this context manager.
        # The code below is used as documentation for future generations of engineers willing to fix this.

        # global bad_layer_handler
        # self.project.setBadLayerHandler(None)
        pass

    def __call__(self, func):
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper
