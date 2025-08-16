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

from typing import Optional

from qgis.PyQt.QtXml import QDomDocument, QDomElement


def get_themapcanvas(doc: QDomDocument) -> Optional[QDomElement]:
    """
    Find the "themapcanvas" DOM element in the QGIS project file.

    NOTE if no QgsGui available, QgsProject::write() will discard the "themapcanvas" element.

    Args:
        doc (QDomDocument): project DOM document

    Returns:
        Optional[QDomElement]: the "themapcanvas" element

    """
    nodes = doc.elementsByTagName("mapcanvas")

    for i in range(nodes.size()):
        node = nodes.item(i)
        el = node.toElement()
        if el.hasAttribute("name") and el.attribute("name") == "theMapCanvas":
            return el

    return None
