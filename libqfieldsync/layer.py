import json
import os
import re
import shutil
from enum import Enum
from typing import ClassVar, Dict, List, Optional

from qgis.core import (
    QgsAttributeEditorField,
    QgsCoordinateTransformContext,
    QgsDataSourceUri,
    QgsFields,
    QgsFileUtils,
    QgsMapLayer,
    QgsProject,
    QgsProviderMetadata,
    QgsProviderRegistry,
    QgsReadWriteContext,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtXml import QDomDocument

from .utils.bad_layer_handler import bad_layer_handler
from .utils.file_utils import slugify
from .utils.logger import logger


class ExpectedVectorLayerError(Exception): ...


class UnsupportedPrimaryKeyError(Exception): ...


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


class SyncAction:
    """Enumeration of sync actions"""

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


class LayerSource:
    class AttachmentType(
        Enum
    ):  # Matches QGIS gui QgsExternalResourceWidget.DocumentViewerContent enum values
        FILE = 0  # QgsExternalResourceWidget.NoContent
        IMAGE = 1  # QgsExternalResourceWidget.Image
        WEB = 2  # QgsExternalResourceWidget.Web
        AUDIO = 3  # QgsExternalResourceWidget.Audio
        VIDEO = 4  # QgsExternalResourceWidget.Video

    class PackagePreventionReason(Enum):
        INVALID = 1
        UNSUPPORTED_DATASOURCE = 2
        LOCALIZED_PATH = 3
        INVALID_REMOTE_RASTER_LAYER = 4

    REASONS_TO_REMOVE_LAYER = (
        PackagePreventionReason.INVALID,
        PackagePreventionReason.UNSUPPORTED_DATASOURCE,
    )

    ATTACHMENT_EXPRESSIONS: ClassVar[Dict[AttachmentType, str]] = {
        AttachmentType.FILE: "'files/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '_{{filename}}'",
        AttachmentType.IMAGE: "'DCIM/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '.{{extension}}'",
        AttachmentType.WEB: "",
        AttachmentType.AUDIO: "'audio/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '.{{extension}}'",
        AttachmentType.VIDEO: "'video/{layername}_' || format_date(now(),'yyyyMMddhhmmsszzz') || '.{{extension}}'",
    }

    def __init__(
        self, layer: QgsMapLayer, project: Optional[QgsProject] = None
    ) -> None:
        if project is None:
            project = QgsProject.instance()

        self.layer = layer
        self._action = None
        self._cloud_action = None
        self._attachment_naming = {}
        # compatibility with QFieldSync <4.3 and QField <2.7
        self._photo_naming = {}
        self._relationship_maximum_visible = {}
        self._value_map_button_interface_threshold = 0
        self._is_feature_addition_locked = None
        self._is_feature_addition_locked_expression_active = False
        self._feature_addition_locked_expression = ""
        self._is_attribute_editing_locked = None
        self._is_attribute_editing_locked_expression_active = False
        self._attribute_editing_locked_expression = ""
        self._is_geometry_editing_locked = None
        self._is_geometry_editing_locked_expression_active = False
        self._geometry_editing_locked_expression = ""
        self._is_feature_deletion_locked = None
        self._is_feature_deletion_locked_expression_active = False
        self._feature_deletion_locked_expression = ""
        self._allow_value_relation_feature_addition = False
        self._tracking_session_active = False
        self._tracking_time_requirement_active = False
        self._tracking_time_requirement_interval_seconds = 30
        self._tracking_distance_requirement_active = False
        self._tracking_distance_requirement_minimum_meters = 30
        self._tracking_sensor_data_requirement_active = False
        self._tracking_all_requirements_active = False
        self._tracking_erroneous_distance_safeguard_active = False
        self._tracking_erroneous_distance_safeguard_maximum_meters = 100
        self._tracking_measurement_type = 0

        self.read_layer()
        self.project = project

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
        self._value_map_button_interface_threshold = self.layer.customProperty(
            "QFieldSync/value_map_button_interface_threshold", 0
        )

        # Compatibility with pre-QFieldSync 4.15.0 where no fine-grained permission lock existed
        if "QFieldSync/is_geometry_locked" in self.layer.customPropertyKeys():
            is_locked = self.layer.customProperty(
                "QFieldSync/is_geometry_locked", False
            )
            is_locked_expression_active = self.layer.customProperty(
                "QFieldSync/is_geometry_locked_expression_active", False
            )
            locked_expression = self.layer.customProperty(
                "QFieldSync/geometry_locked_expression", ""
            )

            self._is_feature_addition_locked = is_locked
            self._is_feature_addition_locked_expression_active = (
                is_locked_expression_active
            )
            self._feature_addition_locked_expression = locked_expression
            self._is_attribute_editing_locked = False
            self._is_attribute_editing_locked_expression_active = False
            self._attribute_editing_locked_expression = ""
            self._is_geometry_editing_locked = is_locked
            self._is_geometry_editing_locked_expression_active = (
                is_locked_expression_active
            )
            self._geometry_editing_locked_expression = locked_expression
            self._is_feature_deletion_locked = is_locked
            self._is_feature_deletion_locked_expression_active = (
                is_locked_expression_active
            )
            self._feature_deletion_locked_expression = locked_expression
        else:
            self._is_feature_addition_locked = self.layer.customProperty(
                "QFieldSync/is_feature_addition_locked", False
            )
            self._is_feature_addition_locked_expression_active = (
                self.layer.customProperty(
                    "QFieldSync/is_feature_addition_locked_expression_active", False
                )
            )
            self._feature_addition_locked_expression = self.layer.customProperty(
                "QFieldSync/feature_addition_locked_expression", ""
            )
            self._is_attribute_editing_locked = self.layer.customProperty(
                "QFieldSync/is_attribute_editing_locked", False
            )
            self._is_attribute_editing_locked_expression_active = (
                self.layer.customProperty(
                    "QFieldSync/is_attribute_editing_locked_expression_active", False
                )
            )
            self._attribute_editing_locked_expression = self.layer.customProperty(
                "QFieldSync/attribute_editing_locked_expression", ""
            )
            self._is_geometry_editing_locked = self.layer.customProperty(
                "QFieldSync/is_geometry_editing_locked", False
            )
            self._is_geometry_editing_locked_expression_active = (
                self.layer.customProperty(
                    "QFieldSync/is_geometry_editing_locked_expression_active", False
                )
            )
            self._geometry_editing_locked_expression = self.layer.customProperty(
                "QFieldSync/geometry_editing_locked_expression", ""
            )
            self._is_feature_deletion_locked = self.layer.customProperty(
                "QFieldSync/is_feature_deletion_locked", False
            )
            self._is_feature_deletion_locked_expression_active = (
                self.layer.customProperty(
                    "QFieldSync/is_feature_deletion_locked_expression_active", False
                )
            )
            self._feature_deletion_locked_expression = self.layer.customProperty(
                "QFieldSync/feature_deletion_locked_expression", ""
            )
        self._allow_value_relation_feature_addition = self.layer.customProperty(
            "QFieldSync/allow_value_relation_feature_addition", False
        )
        self._tracking_session_active = self.layer.customProperty(
            "QFieldSync/tracking_session_active", False
        )
        self._tracking_time_requirement_active = self.layer.customProperty(
            "QFieldSync/tracking_time_requirement_active", False
        )
        self._tracking_time_requirement_interval_seconds = self.layer.customProperty(
            "QFieldSync/tracking_time_requirement_interval_seconds", 30
        )
        self._tracking_distance_requirement_active = self.layer.customProperty(
            "QFieldSync/tracking_distance_requirement_active", False
        )
        self._tracking_distance_requirement_minimum_meters = self.layer.customProperty(
            "QFieldSync/tracking_distance_requirement_minimum_meters", 30
        )
        self._tracking_sensor_data_requirement_active = self.layer.customProperty(
            "QFieldSync/tracking_sensor_data_requirement_active", False
        )
        self._tracking_all_requirements_active = self.layer.customProperty(
            "QFieldSync/tracking_all_requirements_active", False
        )
        self._tracking_erroneous_distance_safeguard_active = self.layer.customProperty(
            "QFieldSync/tracking_erroneous_distance_safeguard_active", False
        )
        self._tracking_erroneous_distance_safeguard_maximum_meters = (
            self.layer.customProperty(
                "QFieldSync/tracking_erroneous_distance_safeguard_maximum_meters", False
            )
        )
        self._tracking_measurement_type = self.layer.customProperty(
            "QFieldSync/tracking_measurement_type", 0
        )

    def apply(self):  # noqa: PLR0912, PLR0915
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
            self.layer.customProperty("QFieldSync/value_map_button_interface_threshold")
            != self.value_map_button_interface_threshold
        )

        has_changed |= (
            bool(self.layer.customProperty("QFieldSync/is_feature_addition_locked"))
            != self.is_feature_addition_locked
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/is_feature_addition_locked_expression_active"
                )
            )
            != self.is_feature_addition_locked_expression_active
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/feature_addition_locked_expression"
                )
            )
            != self.feature_addition_locked_expression
        )
        has_changed |= (
            bool(self.layer.customProperty("QFieldSync/is_attribute_editing_locked"))
            != self.is_attribute_editing_locked
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/is_attribute_editing_locked_expression_active"
                )
            )
            != self.is_attribute_editing_locked_expression_active
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/attribute_editing_locked_expression"
                )
            )
            != self.attribute_editing_locked_expression
        )
        has_changed |= (
            bool(self.layer.customProperty("QFieldSync/is_geometry_editing_locked"))
            != self.is_geometry_editing_locked
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/is_geometry_editing_locked_expression_active"
                )
            )
            != self.is_geometry_editing_locked_expression_active
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/geometry_editing_locked_expression"
                )
            )
            != self.geometry_editing_locked_expression
        )
        has_changed |= (
            bool(self.layer.customProperty("QFieldSync/is_feature_deletion_locked"))
            != self.is_feature_deletion_locked
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/is_feature_deletion_locked_expression_active"
                )
            )
            != self.is_feature_deletion_locked_expression_active
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/feature_deletion_locked_expression"
                )
            )
            != self.feature_deletion_locked_expression
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/allow_value_relation_feature_addition"
                )
            )
            != self.allow_value_relation_feature_addition
        )
        has_changed |= (
            bool(self.layer.customProperty("QFieldSync/tracking_session_active"))
            != self.tracking_session_active
        )
        has_changed |= (
            bool(
                self.layer.customProperty("QFieldSync/tracking_time_requirement_active")
            )
            != self.tracking_time_requirement_active
        )
        has_changed |= (
            self.layer.customProperty(
                "QFieldSync/tracking_time_requirement_interval_seconds"
            )
            != self.tracking_time_requirement_interval_seconds
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/tracking_distance_requirement_active"
                )
            )
            != self.tracking_distance_requirement_active
        )
        has_changed |= (
            self.layer.customProperty(
                "QFieldSync/tracking_distance_requirement_minimum_meters"
            )
            != self.tracking_distance_requirement_minimum_meters
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/tracking_sensor_data_requirement_active"
                )
            )
            != self.tracking_sensor_data_requirement_active
        )
        has_changed |= (
            bool(
                self.layer.customProperty("QFieldSync/tracking_all_requirements_active")
            )
            != self.tracking_all_requirements_active
        )
        has_changed |= (
            bool(
                self.layer.customProperty(
                    "QFieldSync/tracking_erroneous_distance_safeguard_active"
                )
            )
            != self.tracking_erroneous_distance_safeguard_active
        )
        has_changed |= (
            self.layer.customProperty(
                "QFieldSync/tracking_erroneous_distance_safeguard_maximum_meters"
            )
            != self.tracking_erroneous_distance_safeguard_maximum_meters
        )
        has_changed |= (
            self.layer.customProperty("QFieldSync/tracking_measurement_type")
            != self.tracking_measurement_type
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
        self.layer.setCustomProperty(
            "QFieldSync/value_map_button_interface_threshold",
            self.value_map_button_interface_threshold,
        )

        # clear old, outdated properties
        self.layer.removeCustomProperty("QFieldSync/is_geometry_locked")
        self.layer.removeCustomProperty(
            "QFieldSync/is_geometry_locked_expression_active"
        )
        self.layer.removeCustomProperty("QFieldSync/geometry_locked_expression")

        if self.is_feature_addition_locked:
            self.layer.setCustomProperty("QFieldSync/is_feature_addition_locked", True)
        else:
            self.layer.removeCustomProperty("QFieldSync/is_feature_addition_locked")
        if self.is_feature_addition_locked_expression_active:
            self.layer.setCustomProperty(
                "QFieldSync/is_feature_addition_locked_expression_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/is_feature_addition_locked_expression_active"
            )
        self.layer.setCustomProperty(
            "QFieldSync/feature_addition_locked_expression",
            self.feature_addition_locked_expression,
        )
        if self.is_attribute_editing_locked:
            self.layer.setCustomProperty("QFieldSync/is_attribute_editing_locked", True)
        else:
            self.layer.removeCustomProperty("QFieldSync/is_attribute_editing_locked")
        if self.is_attribute_editing_locked_expression_active:
            self.layer.setCustomProperty(
                "QFieldSync/is_attribute_editing_locked_expression_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/is_attribute_editing_locked_expression_active"
            )
        self.layer.setCustomProperty(
            "QFieldSync/attribute_editing_locked_expression",
            self.attribute_editing_locked_expression,
        )
        if self.is_geometry_editing_locked:
            self.layer.setCustomProperty("QFieldSync/is_geometry_editing_locked", True)
        else:
            self.layer.removeCustomProperty("QFieldSync/is_geometry_editing_locked")
        if self.is_geometry_editing_locked_expression_active:
            self.layer.setCustomProperty(
                "QFieldSync/is_geometry_editing_locked_expression_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/is_geometry_editing_locked_expression_active"
            )
        self.layer.setCustomProperty(
            "QFieldSync/geometry_editing_locked_expression",
            self.geometry_editing_locked_expression,
        )
        if self.is_feature_deletion_locked:
            self.layer.setCustomProperty("QFieldSync/is_feature_deletion_locked", True)
        else:
            self.layer.removeCustomProperty("QFieldSync/is_feature_deletion_locked")
        if self.is_feature_deletion_locked_expression_active:
            self.layer.setCustomProperty(
                "QFieldSync/is_feature_deletion_locked_expression_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/is_feature_deletion_locked_expression_active"
            )
        self.layer.setCustomProperty(
            "QFieldSync/feature_deletion_locked_expression",
            self.feature_deletion_locked_expression,
        )

        if self.allow_value_relation_feature_addition:
            self.layer.setCustomProperty(
                "QFieldSync/allow_value_relation_feature_addition", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/allow_value_relation_feature_addition"
            )

        if self.tracking_session_active:
            self.layer.setCustomProperty("QFieldSync/tracking_session_active", True)
        else:
            self.layer.removeCustomProperty("QFieldSync/tracking_session_active")
        if self.tracking_time_requirement_active:
            self.layer.setCustomProperty(
                "QFieldSync/tracking_time_requirement_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/tracking_time_requirement_active"
            )
        self.layer.setCustomProperty(
            "QFieldSync/tracking_time_requirement_interval_seconds",
            self.tracking_time_requirement_interval_seconds,
        )
        if self.tracking_distance_requirement_active:
            self.layer.setCustomProperty(
                "QFieldSync/tracking_distance_requirement_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/tracking_distance_requirement_active"
            )
        self.layer.setCustomProperty(
            "QFieldSync/tracking_distance_requirement_minimum_meters",
            self.tracking_distance_requirement_minimum_meters,
        )
        if self.tracking_sensor_data_requirement_active:
            self.layer.setCustomProperty(
                "QFieldSync/tracking_sensor_data_requirement_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/tracking_sensor_data_requirement_active"
            )
        if self.tracking_all_requirements_active:
            self.layer.setCustomProperty(
                "QFieldSync/tracking_all_requirements_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/tracking_all_requirements_active"
            )
        if self.tracking_erroneous_distance_safeguard_active:
            self.layer.setCustomProperty(
                "QFieldSync/tracking_erroneous_distance_safeguard_active", True
            )
        else:
            self.layer.removeCustomProperty(
                "QFieldSync/tracking_erroneous_distance_safeguard_active"
            )
        self.layer.setCustomProperty(
            "QFieldSync/tracking_erroneous_distance_safeguard_maximum_meters",
            self.tracking_erroneous_distance_safeguard_maximum_meters,
        )
        self.layer.setCustomProperty(
            "QFieldSync/tracking_measurement_type", self.tracking_measurement_type
        )

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
            raise ExpectedVectorLayerError(
                f'Cannot get attachment field types for non-vector layer "{self.layer.name()}"!'
            )

        field_idx = self.layer.fields().indexFromName(field_name)
        ews = self.layer.editorWidgetSetup(field_idx)

        if ews.type() != "ExternalResource":
            return None

        resource_type = ews.config().get("DocumentViewer", 0)
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
        if self.is_virtual:
            return SyncAction.NO_ACTION
        elif self.is_file:
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
    def is_virtual(self):
        return (
            self.layer.dataProvider() and self.layer.dataProvider().name() == "virtual"
        )

    @property
    def available_actions(self):
        actions = []

        if self.is_virtual:
            actions.append(
                (
                    SyncAction.NO_ACTION,
                    QCoreApplication.translate(
                        "LayerAction", "Directly access data source"
                    ),
                )
            )
            return actions

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

        if self.is_virtual or self.is_localized_path:
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

        if self.layer.type() == QgsMapLayer.VectorLayer:
            # all vector layers can be converted for offline editting
            actions.append(
                (
                    SyncAction.OFFLINE,
                    QCoreApplication.translate("LayerAction", "Offline editing"),
                )
            )

            # only online layers support direct access, e.g. PostGIS or WFS
            if not self.is_file or self.layer.readOnly():
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
                if (  # noqa: SIM114
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
        return not self.layer.source().endswith("ecw")

    @property
    def can_lock_geometry(self):
        return self.layer.type() == QgsMapLayer.VectorLayer

    @property
    def value_map_button_interface_threshold(self):
        return self._value_map_button_interface_threshold

    @value_map_button_interface_threshold.setter
    def value_map_button_interface_threshold(
        self, value_map_button_interface_threshold
    ):
        self._value_map_button_interface_threshold = (
            value_map_button_interface_threshold
        )

    @property
    def is_feature_addition_locked(self):
        return bool(self._is_feature_addition_locked)

    @is_feature_addition_locked.setter
    def is_feature_addition_locked(self, is_feature_addition_locked):
        self._is_feature_addition_locked = is_feature_addition_locked

    @property
    def is_feature_addition_locked_expression_active(self):
        return bool(self._is_feature_addition_locked_expression_active)

    @is_feature_addition_locked_expression_active.setter
    def is_feature_addition_locked_expression_active(
        self, is_feature_addition_locked_expression_active
    ):
        self._is_feature_addition_locked_expression_active = (
            is_feature_addition_locked_expression_active
        )

    @property
    def feature_addition_locked_expression(self):
        return self._feature_addition_locked_expression

    @feature_addition_locked_expression.setter
    def feature_addition_locked_expression(self, feature_addition_locked_expression):
        self._feature_addition_locked_expression = feature_addition_locked_expression

    @property
    def is_attribute_editing_locked(self):
        return bool(self._is_attribute_editing_locked)

    @is_attribute_editing_locked.setter
    def is_attribute_editing_locked(self, is_attribute_editing_locked):
        self._is_attribute_editing_locked = is_attribute_editing_locked

    @property
    def is_attribute_editing_locked_expression_active(self):
        return bool(self._is_attribute_editing_locked_expression_active)

    @is_attribute_editing_locked_expression_active.setter
    def is_attribute_editing_locked_expression_active(
        self, is_attribute_editing_locked_expression_active
    ):
        self._is_attribute_editing_locked_expression_active = (
            is_attribute_editing_locked_expression_active
        )

    @property
    def attribute_editing_locked_expression(self):
        return self._attribute_editing_locked_expression

    @attribute_editing_locked_expression.setter
    def attribute_editing_locked_expression(self, attribute_editing_locked_expression):
        self._attribute_editing_locked_expression = attribute_editing_locked_expression

    @property
    def is_geometry_editing_locked(self):
        return bool(self._is_geometry_editing_locked)

    @is_geometry_editing_locked.setter
    def is_geometry_editing_locked(self, is_geometry_editing_locked):
        self._is_geometry_editing_locked = is_geometry_editing_locked

    @property
    def is_geometry_editing_locked_expression_active(self):
        return bool(self._is_geometry_editing_locked_expression_active)

    @is_geometry_editing_locked_expression_active.setter
    def is_geometry_editing_locked_expression_active(
        self, is_geometry_editing_locked_expression_active
    ):
        self._is_geometry_editing_locked_expression_active = (
            is_geometry_editing_locked_expression_active
        )

    @property
    def geometry_editing_locked_expression(self):
        return self._geometry_editing_locked_expression

    @geometry_editing_locked_expression.setter
    def geometry_editing_locked_expression(self, geometry_editing_locked_expression):
        self._geometry_editing_locked_expression = geometry_editing_locked_expression

    @property
    def is_feature_deletion_locked(self):
        return bool(self._is_feature_deletion_locked)

    @is_feature_deletion_locked.setter
    def is_feature_deletion_locked(self, is_feature_deletion_locked):
        self._is_feature_deletion_locked = is_feature_deletion_locked

    @property
    def is_feature_deletion_locked_expression_active(self):
        return bool(self._is_feature_deletion_locked_expression_active)

    @is_feature_deletion_locked_expression_active.setter
    def is_feature_deletion_locked_expression_active(
        self, is_feature_deletion_locked_expression_active
    ):
        self._is_feature_deletion_locked_expression_active = (
            is_feature_deletion_locked_expression_active
        )

    @property
    def feature_deletion_locked_expression(self):
        return self._feature_deletion_locked_expression

    @feature_deletion_locked_expression.setter
    def feature_deletion_locked_expression(self, feature_deletion_locked_expression):
        self._feature_deletion_locked_expression = feature_deletion_locked_expression

    @property
    def allow_value_relation_feature_addition(self):
        return bool(self._allow_value_relation_feature_addition)

    @allow_value_relation_feature_addition.setter
    def allow_value_relation_feature_addition(
        self, allow_value_relation_feature_addition
    ):
        self._allow_value_relation_feature_addition = (
            allow_value_relation_feature_addition
        )

    @property
    def tracking_session_active(self):
        return bool(self._tracking_session_active)

    @tracking_session_active.setter
    def tracking_session_active(self, tracking_session_active):
        self._tracking_session_active = tracking_session_active

    @property
    def tracking_time_requirement_active(self):
        return bool(self._tracking_time_requirement_active)

    @tracking_time_requirement_active.setter
    def tracking_time_requirement_active(self, tracking_time_requirement_active):
        self._tracking_time_requirement_active = tracking_time_requirement_active

    @property
    def tracking_time_requirement_interval_seconds(self):
        return self._tracking_time_requirement_interval_seconds

    @tracking_time_requirement_interval_seconds.setter
    def tracking_time_requirement_interval_seconds(
        self, tracking_time_requirement_interval_seconds
    ):
        self._tracking_time_requirement_interval_seconds = (
            tracking_time_requirement_interval_seconds
        )

    @property
    def tracking_distance_requirement_active(self):
        return bool(self._tracking_distance_requirement_active)

    @tracking_distance_requirement_active.setter
    def tracking_distance_requirement_active(
        self, tracking_distance_requirement_active
    ):
        self._tracking_distance_requirement_active = (
            tracking_distance_requirement_active
        )

    @property
    def tracking_distance_requirement_minimum_meters(self):
        return self._tracking_distance_requirement_minimum_meters

    @tracking_distance_requirement_minimum_meters.setter
    def tracking_distance_requirement_minimum_meters(
        self, tracking_distance_requirement_minimum_meters
    ):
        self._tracking_distance_requirement_minimum_meters = (
            tracking_distance_requirement_minimum_meters
        )

    @property
    def tracking_sensor_data_requirement_active(self):
        return bool(self._tracking_sensor_data_requirement_active)

    @tracking_sensor_data_requirement_active.setter
    def tracking_sensor_data_requirement_active(
        self, tracking_sensor_data_requirement_active
    ):
        self._tracking_sensor_data_requirement_active = (
            tracking_sensor_data_requirement_active
        )

    @property
    def tracking_all_requirements_active(self):
        return bool(self._tracking_all_requirements_active)

    @tracking_all_requirements_active.setter
    def tracking_all_requirements_active(self, tracking_all_requirements_active):
        self._tracking_all_requirements_active = tracking_all_requirements_active

    @property
    def tracking_erroneous_distance_safeguard_active(self):
        return bool(self._tracking_erroneous_distance_safeguard_active)

    @tracking_erroneous_distance_safeguard_active.setter
    def tracking_erroneous_distance_safeguard_active(
        self, tracking_erroneous_distance_safeguard_active
    ):
        self._tracking_erroneous_distance_safeguard_active = (
            tracking_erroneous_distance_safeguard_active
        )

    @property
    def tracking_erroneous_distance_safeguard_maximum_meters(self):
        return self._tracking_erroneous_distance_safeguard_maximum_meters

    @tracking_erroneous_distance_safeguard_maximum_meters.setter
    def tracking_erroneous_distance_safeguard_maximum_meters(
        self, tracking_erroneous_distance_safeguard_maximum_meters
    ):
        self._tracking_erroneous_distance_safeguard_maximum_meters = (
            tracking_erroneous_distance_safeguard_maximum_meters
        )

    @property
    def tracking_measurement_type(self):
        return self._tracking_measurement_type

    @tracking_measurement_type.setter
    def tracking_measurement_type(self, tracking_measurement_type):
        self._tracking_measurement_type = tracking_measurement_type

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
        """
        Returns the filename of the file if the layer is file based. E.g. GPKG, CSV, but not PostGIS, WFS

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
        # on QFieldCloud localized layers will be invalid and therefore we get the layer source from `bad_layer_handler`
        source = bad_layer_handler.invalid_layer_sources_by_id.get(self.layer.id())
        if source:
            return (
                source.startswith("localized:")
                or source.startswith("file:localized:")
                or "url=file:localized:" in source
            )

        path_resolver = self.project.pathResolver()
        path = path_resolver.writePath(self.metadata.get("path", ""))

        return path.startswith("localized:")

    @property
    def is_remote_raster_layer(self) -> bool:
        return bool(
            self.layer.dataProvider() and self.layer.dataProvider().name() == "wms"
        )

    @property
    def package_prevention_reasons(
        self,
    ) -> List["LayerSource.PackagePreventionReason"]:
        reasons = []

        # remove unsupported layers from the packaged project
        if not self.is_supported:
            reasons.append(LayerSource.PackagePreventionReason.UNSUPPORTED_DATASOURCE)

        # The layer is invalid and it is a shared datasource.
        # The project is available locally, but the shared dataset is not found in the shared datasets, probably not downloaded yet.
        if not self.layer.isValid() and self.is_localized_path:
            reasons.append(LayerSource.PackagePreventionReason.LOCALIZED_PATH)
        # sometimes the remote layers are inaccessible from the current network, but we should spare them from removal
        elif not self.layer.isValid() and self.is_remote_raster_layer:
            reasons.append(
                LayerSource.PackagePreventionReason.INVALID_REMOTE_RASTER_LAYER
            )
        # remove invalid layers from the packaged project
        # NOTE localized layers will be always invalid on QFieldCloud
        elif not self.layer.isValid():
            reasons.append(LayerSource.PackagePreventionReason.INVALID)

        return reasons

    @property
    def pk_attr_name(self) -> str:
        try:
            return self.get_pk_attr_name()
        except (ExpectedVectorLayerError, UnsupportedPrimaryKeyError):
            return ""

    def get_pk_attr_name(self) -> str:
        pk_attr_name: str = ""

        if self.layer.type() != QgsMapLayer.VectorLayer:
            raise ExpectedVectorLayerError()

        pk_indexes = self.layer.primaryKeyAttributes()
        fields = self.layer.fields()

        if len(pk_indexes) == 1:
            pk_attr_name = fields[pk_indexes[0]].name()
        elif len(pk_indexes) > 1:
            raise UnsupportedPrimaryKeyError(
                "Composite (multi-column) primary keys are not supported!"
            )
        else:
            logger.info(
                f'Layer "{self.layer.name()}" does not have a primary key. Trying to fallback to `fid`…'
            )

            # NOTE `QgsFields.lookupField(str)` is case insensitive (so we support "fid", "FID", "Fid" etc),
            # but also looks for the field alias, that's why we check the `field.name().lower() == "fid"`
            fid_idx = fields.lookupField("fid")
            if fid_idx >= 0 and fields.at(fid_idx).name().lower() == "fid":
                fid_name = fields.at(fid_idx).name()
                logger.info(
                    f'Layer "{self.layer.name()}" does not have a primary key so it uses the `fid` attribute as a fallback primary key. '
                    "This is an unstable feature! "
                    "Consider [converting to GeoPackages instead](https://docs.qfield.org/get-started/tutorials/get-started-qfc/#configure-your-project-layers-for-qfield). "
                )
                pk_attr_name = fid_name

        if not pk_attr_name:
            raise UnsupportedPrimaryKeyError(
                f'Layer "{self.layer.name()}" neither has a primary key, nor an attribute `fid`! '
            )

        if "," in pk_attr_name:
            raise UnsupportedPrimaryKeyError(
                f'Comma in field name "{pk_attr_name}" is not allowed!'
            )

        logger.info(
            f'Layer "{self.layer.name()}" will use attribute "{pk_attr_name}" as a primary key.'
        )

        return pk_attr_name

    def copy(self, target_path, copied_files, keep_existent=False):
        """
        Copy a layer to a new path and adjust its datasource.

        :param layer: The layer to copy
        :param target_path: A path to a folder into which the data will be copied
        :param keep_existent: if True and target file already exists, keep it as it is
        """
        if not self.is_file:
            # Copy will also be called on non-file layers like WMS. In this case, just do nothing.
            return None

        suffix = ""
        uri_parts = self.layer.source().split("|", 1)
        if len(uri_parts) > 1:
            suffix = uri_parts[1]

        if self.is_file:
            files_to_copy = QgsFileUtils.sidecarFilesForPath(self.filename)
            files_to_copy.add(self.filename)

            for file_to_copy in files_to_copy:
                source_path, file_name = os.path.split(file_to_copy)
                dest_file = os.path.join(target_path, file_name)
                if keep_existent is False or not os.path.isfile(dest_file):
                    shutil.copy(os.path.join(source_path, file_name), dest_file)

            source_path, file_name = os.path.split(self.filename)
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
                        new_source = "{}|{}".format(new_source, suffix)  # noqa: UP032

            self._change_data_source(new_source)
        return copied_files

    def convert_to_gpkg(self, target_path):  # noqa: PLR0912, PLR0915
        """
        Convert a layer to geopackage in the target path and adjust its datasource. If
        a layer is already a geopackage, the dataset will merely be copied to the target
        path.

        :param layer: The layer to copy
        :param target_path: A path to a folder into which the data will be copied
        :param keep_existent: if True and target file already exists, keep it as it is
        """
        if self.layer.type() != QgsMapLayer.VectorLayer or not self.layer.isValid():
            return None

        assert isinstance(self.layer, QgsVectorLayer)

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
                    new_source = "{}|{}".format(new_source, suffix)  # noqa: UP032

        layer_subset_string = self.layer.subsetString()
        if new_source == "":
            pattern = re.compile(r"[\W_]+")
            cleaned_name = pattern.sub("", self.layer.name())
            dest_file = os.path.join(target_path, f"{cleaned_name}.gpkg")
            suffix = 0
            while os.path.isfile(dest_file):
                suffix += 1
                dest_file = os.path.join(target_path, f"{cleaned_name}_{suffix}.gpkg")

            # clone vector layer and strip it of filter, joins, and virtual fields
            source_layer = self.layer.clone()

            assert source_layer is not None

            source_layer.setSubsetString("")
            source_layer_joins = source_layer.vectorJoins()
            for join in source_layer_joins:
                source_layer.removeJoin(join.joinLayerId())
            fields = source_layer.fields()
            virtual_field_count = 0
            for i in range(len(fields)):
                if fields.fieldOrigin(i) == QgsFields.OriginExpression:
                    source_layer.removeExpressionField(i - virtual_field_count)
                    virtual_field_count += 1

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.fileEncoding = "UTF-8"
            options.driverName = "GPKG"
            (
                error,
                _error_msg,
                returned_dest_file,
                _returned_dest_layer,
            ) = QgsVectorFileWriter.writeAsVectorFormatV3(
                source_layer, dest_file, QgsCoordinateTransformContext(), options
            )

            if error != QgsVectorFileWriter.NoError:
                return None
            if returned_dest_file:
                new_source = returned_dest_file
            else:
                new_source = dest_file

        self._change_data_source(new_source, "ogr")
        if layer_subset_string:
            self.layer.setSubsetString(layer_subset_string)

        return dest_file

    def _change_data_source(self, new_data_source, new_provider=None):
        """Changes the datasource string of the layer"""
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
