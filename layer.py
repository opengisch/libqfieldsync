import json
import os
import re
import shutil

from qgis.core import (
    Qgis,
    QgsAttributeEditorField,
    QgsCoordinateTransformContext,
    QgsDataSourceUri,
    QgsFields,
    QgsMapLayer,
    QgsProject,
    QgsProviderRegistry,
    QgsReadWriteContext,
    QgsVectorFileWriter,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtXml import QDomDocument

from .utils.file_utils import slugify

# When copying files, if any of the extension in any of the groups is found,
# other files with the same extension in the same folder will be copied as well.
file_extension_groups = [
    [
        ".shp",
        ".shx",
        ".dbf",
        ".sbx",
        ".sbn",
        ".shp.xml",
        ".prj",
        ".cpg",
        ".qpj",
        ".qix",
    ],
    [".tab", ".dat", ".map", ".xls", ".xlsx", ".id", ".ind", ".wks", ".dbf"],
    [".png", ".pgw"],
    [".jpg", ".jgw"],
    [".tif", ".tfw", ".wld"],
]


def get_file_extension_group(filename):
    """
    Return the basename and an extension group (if applicable)

    Examples:
         airports.shp -> 'airport', ['.shp', '.shx', '.dbf', '.sbx', '.sbn', '.shp.xml']
         forests.gpkg -> 'forests', ['.gpkg']
    """
    for group in file_extension_groups:
        for extension in group:
            if filename.endswith(extension):
                return filename[: -len(extension)], group
    basename, ext = os.path.splitext(filename)
    return basename, [ext]


class SyncAction(object):
    """
    Enumeration of sync actions
    """

    # Make an offline editing copy
    def __init__(self):
        raise RuntimeError("Should only be used as enumeration")

    # Take an offline editing copy of this layer
    OFFLINE = "offline"

    # No action for online DB layers
    # - will in general leave the source untouched
    NO_ACTION = "no_action"

    # Copy action for file based layers
    # - will be made relative
    # - the file(s) will be copied
    COPY = "copy"

    # Keep already copied data or files if existent
    KEEP_EXISTENT = "keep_existent"

    # remove from the project
    REMOVE = "remove"


class LayerSource(object):
    def __init__(self, layer):
        self.layer = layer
        self._action = None
        self._cloud_action = None
        self._photo_naming = {}
        self._is_geometry_locked = None
        self.read_layer()

        self.storedInlocalizedDataPath = False
        if self.layer.dataProvider() is not None:
            pathResolver = QgsProject.instance().pathResolver()
            metadata = QgsProviderRegistry.instance().providerMetadata(
                self.layer.dataProvider().name()
            )
            if metadata is not None:
                decoded = metadata.decodeUri(self.layer.source())
                if "path" in decoded:
                    path = pathResolver.writePath(decoded["path"])
                    if path.startswith("localized:"):
                        self.storedInlocalizedDataPath = True

    def read_layer(self):
        self._action = self.layer.customProperty("QFieldSync/action")
        self._cloud_action = self.layer.customProperty("QFieldSync/cloud_action")
        self._photo_naming = json.loads(
            self.layer.customProperty("QFieldSync/photo_naming") or "{}"
        )
        self._is_geometry_locked = self.layer.customProperty(
            "QFieldSync/is_geometry_locked", False
        )

    def apply(self):
        photo_naming_json = json.dumps(self._photo_naming)

        has_changed = False
        has_changed |= self.layer.customProperty("QFieldSync/action") != self.action
        has_changed |= (
            self.layer.customProperty("QFieldSync/cloud_action") != self.cloud_action
        )
        has_changed |= (
            self.layer.customProperty("QFieldSync/photo_naming") != photo_naming_json
        )
        has_changed |= (
            bool(self.layer.customProperty("QFieldSync/is_geometry_locked"))
            != self.is_geometry_locked
        )

        self.layer.setCustomProperty("QFieldSync/action", self.action)
        self.layer.setCustomProperty("QFieldSync/cloud_action", self.cloud_action)
        self.layer.setCustomProperty("QFieldSync/photo_naming", photo_naming_json)

        # custom properties does not store the data type, so it is safer to remove boolean custom properties, rather than setting them to the string 'false' (which is boolean `True`)
        if self.is_geometry_locked:
            self.layer.setCustomProperty("QFieldSync/is_geometry_locked", True)
        else:
            self.layer.removeCustomProperty("QFieldSync/is_geometry_locked")

        return has_changed

    @property
    def action(self):
        if self._action is None:
            return self.default_action
        else:
            return self._action

    @action.setter
    def action(self, action):
        self._action = action

    @property
    def cloud_action(self):
        if self._cloud_action is None:
            return self.default_cloud_action
        else:
            return self._cloud_action

    @cloud_action.setter
    def cloud_action(self, action):
        self._cloud_action = action

    def photo_naming(self, field_name: str) -> str:
        return self._photo_naming.get(
            field_name,
            "'DCIM/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '.jpg'".format(
                layername=slugify(self.layer.name())
            ),
        )

    def set_photo_naming(self, field_name: str, expression: str):
        self._photo_naming[field_name] = expression

    @property
    def default_action(self):
        if self.is_file:
            return SyncAction.COPY
        elif not self.is_supported:
            return SyncAction.REMOVE
        elif self.layer.providerType() == "postgres":
            return SyncAction.OFFLINE
        else:
            return SyncAction.NO_ACTION

    @property
    def default_cloud_action(self):
        return self.available_cloud_actions[0][0]

    @property
    def is_configured(self):
        return self._action is not None

    @property
    def is_file(self):
        if self.layer.dataProvider() is not None:
            metadata = QgsProviderRegistry.instance().providerMetadata(
                self.layer.dataProvider().name()
            )
            if metadata is not None:
                decoded = metadata.decodeUri(self.layer.source())
                if "path" in decoded:
                    if os.path.isfile(decoded["path"]):
                        return True
        return False

    @property
    def available_actions(self):
        actions = list()

        if self.is_file and not self.storedInlocalizedDataPath:
            actions.append(
                (SyncAction.COPY, QCoreApplication.translate("LayerAction", "Copy"))
            )
            actions.append(
                (
                    SyncAction.KEEP_EXISTENT,
                    QCoreApplication.translate(
                        "LayerAction", "Keep existent (Copy if missing)"
                    ),
                )
            )
        else:
            actions.append(
                (
                    SyncAction.NO_ACTION,
                    QCoreApplication.translate(
                        "LayerAction", "Directly access data source"
                    ),
                )
            )

        if self.layer.type() == QgsMapLayer.VectorLayer:
            actions.append(
                (
                    SyncAction.OFFLINE,
                    QCoreApplication.translate("LayerAction", "Offline editing"),
                )
            )

        actions.append(
            (
                SyncAction.REMOVE,
                QCoreApplication.translate("LayerAction", "Remove from project"),
            )
        )

        return actions

    @property
    def available_cloud_actions(self):
        actions = []

        if self.layer.type() == QgsMapLayer.VectorLayer:
            # all vector layers can be converted for offline editting
            actions.append(
                (
                    SyncAction.OFFLINE,
                    QCoreApplication.translate("LayerAction", "Offline editing"),
                )
            )

            # only online layers support direct access, e.g. PostGIS or WFS
            if not (self.is_file and not self.storedInlocalizedDataPath):
                actions.append(
                    (
                        SyncAction.NO_ACTION,
                        QCoreApplication.translate(
                            "LayerAction", "Directly access data source"
                        ),
                    )
                )
            elif self.is_file and not self.storedInlocalizedDataPath:
                actions.append(
                    (
                        SyncAction.NO_ACTION,
                        QCoreApplication.translate(
                            "LayerAction", "Directly access data source"
                        ),
                    )
                )
        else:
            # all other layers support direct acess too, e.g. rasters, WMS, XYZ etc
            actions.append(
                (
                    SyncAction.NO_ACTION,
                    QCoreApplication.translate(
                        "LayerAction", "Directly access data source"
                    ),
                )
            )

        actions.append(
            (
                SyncAction.REMOVE,
                QCoreApplication.translate("LayerAction", "Remove from project"),
            )
        )

        return actions

    def preferred_cloud_action(self, prefer_online):
        actions = self.available_cloud_actions

        for idx, (action, _text) in enumerate(actions):
            if prefer_online:
                if action == SyncAction.NO_ACTION:
                    return idx, action
            else:
                if (
                    (self.is_file and not self.storedInlocalizedDataPath)
                    or self.layer.type() != QgsMapLayer.VectorLayer
                ) and action == SyncAction.NO_ACTION:
                    return idx, action
                elif action == SyncAction.OFFLINE:
                    return idx, action

        return (-1, None)

    @property
    def is_supported(self):
        # ecw raster
        if self.layer.source().endswith("ecw"):
            return False
        else:
            return True

    @property
    def can_lock_geometry(self):
        return self.layer.type() == QgsMapLayer.VectorLayer

    @property
    def is_geometry_locked(self):
        return bool(self._is_geometry_locked)

    @is_geometry_locked.setter
    def is_geometry_locked(self, is_geometry_locked):
        self._is_geometry_locked = is_geometry_locked

    @property
    def warning(self):
        if self.layer.source().endswith("ecw"):
            return QCoreApplication.translate(
                "DataSourceWarning", "ECW layers are not supported by QField."
            )
        return None

    @property
    def name(self):
        return self.layer.name()

    def copy(self, target_path, copied_files, keep_existent=False):
        """
        Copy a layer to a new path and adjust its datasource.

        :param layer: The layer to copy
        :param target_path: A path to a folder into which the data will be copied
        :param keep_existent: if True and target file already exists, keep it as it is
        """
        if not self.is_file:
            # Copy will also be called on non-file layers like WMS. In this case, just do nothing.
            return

        parts = None
        file_path = ""
        suffix = ""
        uri_parts = self.layer.source().split("|", 1)
        if len(uri_parts) > 1:
            suffix = uri_parts[1]

        if self.layer.dataProvider() is not None:
            metadata = QgsProviderRegistry.instance().providerMetadata(
                self.layer.dataProvider().name()
            )
            if metadata is not None:
                parts = metadata.decodeUri(self.layer.source())
                if "path" in parts:
                    file_path = parts["path"]
        if file_path == "":
            file_path = uri_parts[0]

        if os.path.isfile(file_path):
            source_path, file_name = os.path.split(file_path)
            basename, extensions = get_file_extension_group(file_name)
            for ext in extensions:
                dest_file = os.path.join(target_path, basename + ext)
                if os.path.exists(os.path.join(source_path, basename + ext)) and (
                    keep_existent is False or not os.path.isfile(dest_file)
                ):
                    shutil.copy(os.path.join(source_path, basename + ext), dest_file)

            new_source = ""
            if Qgis.QGIS_VERSION_INT >= 31200 and self.layer.dataProvider() is not None:
                metadata = QgsProviderRegistry.instance().providerMetadata(
                    self.layer.dataProvider().name()
                )
                if metadata is not None:
                    parts["path"] = os.path.join(target_path, file_name)
                    new_source = metadata.encodeUri(parts)
            if new_source == "":
                if (
                    self.layer.dataProvider()
                    and self.layer.dataProvider().name == "spatialite"
                ):
                    uri = QgsDataSourceUri()
                    uri.setDatabase(os.path.join(target_path, file_name))
                    uri.setTable(parts["layerName"])
                    new_source = uri.uri()
                else:
                    new_source = os.path.join(target_path, file_name)
                    if suffix != "":
                        new_source = "{}|{}".format(new_source, suffix)

            self._change_data_source(new_source)
        return copied_files

    def convert_to_gpkg(self, target_path):
        """
        Convert a layer to geopackage in the target path and adjust its datasource. If
        a layer is already a geopackage, the dataset will merely be copied to the target
        path.

        :param layer: The layer to copy
        :param target_path: A path to a folder into which the data will be copied
        :param keep_existent: if True and target file already exists, keep it as it is
        """

        if not self.layer.type() == QgsMapLayer.VectorLayer or not self.layer.isValid():
            return

        parts = None
        file_path = ""
        suffix = ""
        uri_parts = self.layer.source().split("|", 1)
        if len(uri_parts) > 1:
            suffix = uri_parts[1]

        metadata = QgsProviderRegistry.instance().providerMetadata(
            self.layer.dataProvider().name()
        )
        if metadata is not None:
            parts = metadata.decodeUri(self.layer.source())
            if "path" in parts:
                file_path = parts["path"]
        if file_path == "":
            file_path = uri_parts[0]

        dest_file = ""
        new_source = ""
        # check if the source is a geopackage, and merely copy if it's the case
        if (
            os.path.isfile(file_path)
            and self.layer.dataProvider().storageType() == "GPKG"
        ):
            source_path, file_name = os.path.split(file_path)
            dest_file = os.path.join(target_path, file_name)
            if not os.path.isfile(dest_file):
                shutil.copy(os.path.join(source_path, file_name), dest_file)

            if Qgis.QGIS_VERSION_INT >= 31200:
                metadata = QgsProviderRegistry.instance().providerMetadata(
                    self.layer.dataProvider().name()
                )
                if metadata is not None:
                    parts["path"] = dest_file
                    new_source = metadata.encodeUri(parts)
            if new_source == "":
                new_source = os.path.join(target_path, file_name)
                if suffix != "":
                    new_source = "{}|{}".format(new_source, suffix)

        layer_subset_string = self.layer.subsetString()
        if new_source == "":
            pattern = re.compile("[\W_]+")  # NOQA
            cleaned_name = pattern.sub("", self.layer.name())
            dest_file = os.path.join(target_path, "{}.gpkg".format(cleaned_name))
            suffix = 0
            while os.path.isfile(dest_file):
                suffix += 1
                dest_file = os.path.join(
                    target_path, "{}_{}.gpkg".format(cleaned_name, suffix)
                )

            # clone vector layer and strip it of filter, joins, and virtual fields
            source_layer = self.layer.clone()
            source_layer.setSubsetString("")
            source_layer_joins = source_layer.vectorJoins()
            for join in source_layer_joins:
                source_layer.removeJoin(join.joinLayerId())
            fields = source_layer.fields()
            virtual_field_count = 0
            for i in range(0, len(fields)):
                if fields.fieldOrigin(i) == QgsFields.OriginExpression:
                    source_layer.removeExpressionField(i - virtual_field_count)
                    virtual_field_count += 1

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.fileEncoding = "UTF-8"
            options.driverName = "GPKG"
            (error, dest_file) = QgsVectorFileWriter.writeAsVectorFormatV2(
                source_layer, dest_file, QgsCoordinateTransformContext(), options
            )
            if error != QgsVectorFileWriter.NoError:
                return
            new_source = dest_file

        self._change_data_source(new_source, "ogr")
        if layer_subset_string:
            self.layer.setSubsetString(layer_subset_string)

        return dest_file

    def _change_data_source(self, new_data_source, new_provider=None):
        """
        Changes the datasource string of the layer
        """
        context = QgsReadWriteContext()
        document = QDomDocument("style")
        map_layers_element = document.createElement("maplayers")
        map_layer_element = document.createElement("maplayer")
        self.layer.writeLayerXml(map_layer_element, document, context)

        # modify DOM element with new layer reference
        map_layer_element.firstChildElement("datasource").firstChild().setNodeValue(
            new_data_source
        )
        map_layers_element.appendChild(map_layer_element)
        document.appendChild(map_layers_element)

        if new_provider:
            map_layer_element.firstChildElement("provider").setAttribute(
                "encoding", "UTF-8"
            )
            map_layer_element.firstChildElement("provider").firstChild().setNodeValue(
                new_provider
            )

        # reload layer definition
        self.layer.readLayerXml(map_layer_element, context)
        self.layer.reload()

    def visible_fields_names(self, items=None):
        if items is None:
            items = self.layer.editFormConfig().tabs()

        fields = self.layer.fields()
        result = []

        for item in items:
            if hasattr(item, "children"):
                result += self.visible_fields_names(item.children())
            elif isinstance(item, QgsAttributeEditorField):
                if item.idx() >= 0:
                    result.append(fields.at(item.idx()).name())

        return result
