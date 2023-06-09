import json
import os
import re
import shutil
from enum import Enum
from typing import Dict, Optional

from qgis.core import (
    QgsAttributeEditorField,
    QgsCoordinateTransformContext,
    QgsDataSourceUri,
    QgsFields,
    QgsMapLayer,
    QgsProject,
    QgsProviderMetadata,
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
    class AttachmentType(
        Enum
    ):  # Matches QGIS gui QgsExternalResourceWidget.DocumentViewerContent enum values
        FILE = 0  # QgsExternalResourceWidget.NoContent
        IMAGE = 1  # QgsExternalResourceWidget.Image
        WEB = 2  # QgsExternalResourceWidget.Web
        AUDIO = 3  # QgsExternalResourceWidget.Audio
        VIDEO = 4  # QgsExternalResourceWidget.Video

    ATTACHMENT_EXPRESSIONS = {
        AttachmentType.FILE: "'files/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '_{{filename}}'",
        AttachmentType.IMAGE: "'DCIM/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '.{{extension}}'",
        AttachmentType.WEB: "",
        AttachmentType.AUDIO: "'audio/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '.{{extension}}'",
        AttachmentType.VIDEO: "'video/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '.{{extension}}'",
    }

    def __init__(self, layer):
        self.layer = layer
        self._action = None
        self._cloud_action = None
        self._attachment_naming = {}
        # compatibility with QFieldSync <4.3 and QField <2.7
        self._photo_naming = {}
        self._relationship_maximum_visible = {}
        self._is_geometry_locked = None
        self.read_layer()
        self.project = QgsProject.instance()

    def read_layer(self):
        self._action = self.layer.customProperty("QFieldSync/action")
        self._cloud_action = self.layer.customProperty("QFieldSync/cloud_action")
        self._attachment_naming = json.loads(
            self.layer.customProperty("QFieldSync/attachment_naming") or "{}"
        )
        # compatibility with QFieldSync <4.3 and QField <2.7
        self._photo_naming = json.loads(
            self.layer.customProperty("QFieldSync/photo_naming") or "{}"
        )
        self._relationship_maximum_visible = json.loads(
            self.layer.customProperty("QFieldSync/relationship_maximum_visible") or "{}"
        )
        self._is_geometry_locked = self.layer.customProperty(
            "QFieldSync/is_geometry_locked", False
        )

    def apply(self):
        attachment_naming_json = json.dumps(self._attachment_naming)
        # compatibility with QFieldSync <4.3 and QField <2.7
        photo_naming_json = json.dumps(self._photo_naming)

        relationship_maximum_visible_json = json.dumps(
            self._relationship_maximum_visible
        )

        has_changed = False
        has_changed |= self.layer.customProperty("QFieldSync/action") != self.action
        has_changed |= (
            self.layer.customProperty("QFieldSync/cloud_action") != self.cloud_action
        )
        has_changed |= (
            self.layer.customProperty("QFieldSync/attachment_naming")
            != attachment_naming_json
        )
        has_changed |= (
            self.layer.customProperty("QFieldSync/relationship_maximum_visible")
            != relationship_maximum_visible_json
        )
        has_changed |= (
            bool(self.layer.customProperty("QFieldSync/is_geometry_locked"))
            != self.is_geometry_locked
        )

        self.layer.setCustomProperty("QFieldSync/action", self.action)
        self.layer.setCustomProperty("QFieldSync/cloud_action", self.cloud_action)
        self.layer.setCustomProperty(
            "QFieldSync/attachment_naming", attachment_naming_json
        )
        # compatibility with QFieldSync <4.3 and QField <2.7
        self.layer.setCustomProperty("QFieldSync/photo_naming", photo_naming_json)
        self.layer.setCustomProperty(
            "QFieldSync/relationship_maximum_visible", relationship_maximum_visible_json
        )

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

    def get_attachment_field_type(self, field_name: str) -> Optional[AttachmentType]:
        if self.layer.type() != QgsMapLayer.VectorLayer:
            return None

        field_idx = self.layer.fields().indexFromName(field_name)
        ews = self.layer.editorWidgetSetup(field_idx)

        if ews.type() != "ExternalResource":
            return None

        resource_type = (
            ews.config()["DocumentViewer"] if "DocumentViewer" in ews.config() else 0
        )
        return self.get_attachment_type_by_int_value(resource_type)

    def get_attachment_fields(self) -> Dict[str, AttachmentType]:
        if self.layer.type() != QgsMapLayer.VectorLayer:
            return {}

        attachment_fields = {}

        for field in self.layer.fields():
            field_name = field.name()
            attachment_type = self.get_attachment_field_type(field_name)

            if attachment_type:
                attachment_fields[field_name] = attachment_type

        return attachment_fields

    def get_attachment_type_by_int_value(self, value: int) -> AttachmentType:
        try:
            return LayerSource.AttachmentType(value)
        except ValueError:
            return LayerSource.AttachmentType.FILE

    def attachment_naming(self, field_name) -> str:
        attachment_type = self.get_attachment_field_type(field_name)
        assert attachment_type is not None
        default_name_setting_value = self.ATTACHMENT_EXPRESSIONS[
            attachment_type
        ].format(layername=slugify(self.layer.name()))

        # compatibility with QFieldSync <4.3 and QField <2.7
        legacy_name_setting_value = None
        if attachment_type == LayerSource.AttachmentType.IMAGE:
            legacy_name_setting_value = self._photo_naming.get(field_name)

        return self._attachment_naming.get(
            field_name,
            legacy_name_setting_value or default_name_setting_value,
        )

    def set_attachment_naming(self, field_name: str, expression: str):
        # compatibility with QFieldSync <4.3 and QField <2.7
        attachment_type = self.get_attachment_field_type(field_name)
        if attachment_type == LayerSource.AttachmentType.IMAGE:
            self._photo_naming[field_name] = expression

        self._attachment_naming[field_name] = expression

    def relationship_maximum_visible(self, relation_id: str) -> int:
        return self._relationship_maximum_visible.get(
            relation_id,
            4,
        )

    def set_relationship_maximum_visible(
        self, relation_id: str, relationship_maximum_visible: int
    ):
        self._relationship_maximum_visible[relation_id] = relationship_maximum_visible

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
    def is_cloud_configured(self):
        return self._cloud_action is not None

    @property
    def is_file(self):
        return os.path.isfile(self.filename)

    @property
    def available_actions(self):
        actions = list()

        if self.is_file and not self.is_localized_path:
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
            if not (self.is_file and not self.is_localized_path):
                actions.append(
                    (
                        SyncAction.NO_ACTION,
                        QCoreApplication.translate(
                            "LayerAction", "Directly access data source"
                        ),
                    )
                )
            elif self.is_file and not self.is_localized_path:
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
                    (self.is_file and not self.is_localized_path)
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

    @property
    def provider_metadata(self) -> Optional[QgsProviderMetadata]:
        if not self.layer.dataProvider():
            return None

        return QgsProviderRegistry.instance().providerMetadata(
            self.layer.dataProvider().name()
        )

    @property
    def metadata(self) -> Dict:
        if self.provider_metadata is None:
            return {}

        return self.provider_metadata.decodeUri(self.layer.source())

    @property
    def filename(self) -> str:
        """Returns the filename of the file if the layer is file based. E.g. GPKG, CSV, but not PostGIS, WFS

        Note: This may return garbage path, e.g. on online layers such as PostGIS or WFS. Always check with os.path.isfile(),
        as Path.is_file() raises an exception prior to Python 3.8
        """
        metadata = self.metadata
        filename = ""

        if self.layer.type() == QgsMapLayer.VectorTileLayer:
            uri = QgsDataSourceUri()
            uri.setEncodedUri(self.layer.source())
            return uri.param("url")
        elif self.layer.dataProvider() is None:
            return ""

        filename = metadata.get("path", "")
        path_resolver = self.project.pathResolver()
        resolved_filename = path_resolver.writePath(filename)
        if resolved_filename.startswith("localized:"):
            return resolved_filename[10:]

        return filename

    @property
    def is_localized_path(self) -> bool:
        path_resolver = self.project.pathResolver()
        path = path_resolver.writePath(self.filename)

        return path.startswith("localized:")

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

        suffix = ""
        uri_parts = self.layer.source().split("|", 1)
        if len(uri_parts) > 1:
            suffix = uri_parts[1]

        if self.is_file:
            source_path, file_name = os.path.split(self.filename)
            basename, extensions = get_file_extension_group(file_name)
            for ext in extensions:
                dest_file = os.path.join(target_path, basename + ext)
                if os.path.exists(os.path.join(source_path, basename + ext)) and (
                    keep_existent is False or not os.path.isfile(dest_file)
                ):
                    shutil.copy(os.path.join(source_path, basename + ext), dest_file)

            new_source = ""
            metadata = self.metadata

            if self.provider_metadata:
                metadata["path"] = os.path.join(target_path, file_name)
                new_source = self.provider_metadata.encodeUri(metadata)

            if new_source == "":
                if (
                    self.layer.dataProvider()
                    and self.layer.dataProvider().name == "spatialite"
                ):
                    uri = QgsDataSourceUri()
                    uri.setDatabase(os.path.join(target_path, file_name))
                    uri.setTable(metadata["layerName"])
                    new_source = uri.uri()
                elif self.layer.type() == QgsMapLayer.VectorTileLayer:
                    uri = QgsDataSourceUri()
                    uri.setEncodedUri(self.layer.source())
                    uri.setParam("url", os.path.join(target_path, file_name))
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

        file_path = self.filename
        suffix = ""
        uri_parts = self.layer.source().split("|", 1)
        if len(uri_parts) > 1:
            suffix = uri_parts[1]

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

            if self.provider_metadata is not None:
                metadata = self.metadata
                metadata["path"] = dest_file
                new_source = self.provider_metadata.encodeUri(metadata)

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
            (error, returned_dest_file) = QgsVectorFileWriter.writeAsVectorFormatV2(
                source_layer, dest_file, QgsCoordinateTransformContext(), options
            )
            if error != QgsVectorFileWriter.NoError:
                return
            if returned_dest_file:
                new_source = returned_dest_file
            else:
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
