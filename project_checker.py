import re
import sys
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from qfieldsync.libqfieldsync.layer import (
    LayerSource,
    SyncAction,
    UnsupportedPrimaryKeyError,
)
from qfieldsync.libqfieldsync.project import ProjectConfiguration, ProjectProperties
from qfieldsync.libqfieldsync.utils.file_utils import isascii
from qgis.core import Qgis, QgsMapLayer, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QObject

from .offline_converter import ExportType

if sys.version_info >= (3, 8):
    from typing import TypedDict
else:
    TypedDict = Dict


class FeedbackResult:
    def __init__(self, message: str) -> None:
        self.message = message


class Feedback:
    class Level(Enum):
        ERROR = "ERROR"
        WARNING = "WARNING"

    def __init__(
        self, level: Level, feedback_result: FeedbackResult, layer: QgsMapLayer = None
    ) -> None:
        self.level = level
        self.message = feedback_result.message
        self.layer_id = layer.id() if layer else None
        self.layer_name = layer.name() if layer else None


class ProjectCheckerFeedback:

    tr = QObject().tr

    def __init__(self) -> None:
        self.feedbacks: Dict[str, List[Feedback]] = {
            # if the key is "", it is considered as project feedback
            "": [],
        }
        self.count = 0
        self.error_feedbacks: List[Feedback] = []
        self.longest_level_name = len(Feedback.Level.WARNING.value)

    def add(self, feedback: Feedback):
        # if the key is "", it is considered as project feedback
        layer_id_key = feedback.layer_id or ""
        self.count += 1
        self.feedbacks[layer_id_key] = self.feedbacks.get(layer_id_key, [])
        self.feedbacks[layer_id_key].append(feedback)

        if feedback.level == Feedback.Level.ERROR:
            self.error_feedbacks.append(feedback)


class ProjectChecker:

    tr = QObject().tr

    class CheckConfig(TypedDict):
        type: Feedback.Level
        fn: Callable
        scope: Optional[ExportType]

    def __init__(self, project: QgsProject) -> None:
        self.project = project
        self.project_checks: List[ProjectChecker.CheckConfig] = [
            {
                "type": Feedback.Level.ERROR,
                "fn": self.check_no_absolute_filepaths,
                "scope": None,
            },
            {
                "type": Feedback.Level.ERROR,
                "fn": self.check_no_homepath,
                "scope": None,
            },
            {
                "type": Feedback.Level.ERROR,
                "fn": self.check_files_have_unsupported_characters,
                "scope": None,
            },
            {
                "type": Feedback.Level.WARNING,
                "fn": self.check_project_is_dirty,
                "scope": ExportType.Cloud,
            },
        ]
        self.layer_checks: List[ProjectChecker.CheckConfig] = [
            {
                "type": Feedback.Level.WARNING,
                "fn": self.check_layer_has_utf8_datasources,
                "scope": None,
            },
            {
                "type": Feedback.Level.WARNING,
                "fn": self.check_layer_has_ascii_filename,
                "scope": None,
            },
            {
                "type": Feedback.Level.WARNING,
                "fn": self.check_layer_primary_key,
                "scope": ExportType.Cloud,
            },
            {
                "type": Feedback.Level.WARNING,
                "fn": self.check_layer_memory,
                "scope": None,
            },
            {
                "type": Feedback.Level.WARNING,
                "fn": self.check_layer_configured,
                "scope": None,
            },
            {
                "type": Feedback.Level.WARNING,
                "fn": self.check_layer_package_prevention,
                "scope": None,
            },
        ]

    def check(self, scope: ExportType) -> ProjectCheckerFeedback:
        checked_feedback = ProjectCheckerFeedback()

        for check in self.project_checks:
            if check["scope"] and check["scope"] != scope:
                continue

            feedback_result = check["fn"]()
            if feedback_result:
                checked_feedback.add(Feedback(check["type"], feedback_result))

        for layer in self.project.mapLayers().values():
            layer_source = LayerSource(layer)

            if (
                layer_source.action == SyncAction.REMOVE
                and layer_source.cloud_action == SyncAction.REMOVE
            ):
                continue

            for check in self.layer_checks:
                if (
                    check["scope"] == ExportType.Cable
                    and layer_source.action == SyncAction.REMOVE
                ) or (
                    check["scope"] == ExportType.Cloud
                    and layer_source.cloud_action == SyncAction.REMOVE
                ):
                    break

                if check["scope"] and check["scope"] != scope:
                    continue

                feedback_result = check["fn"](layer_source)
                if feedback_result:
                    checked_feedback.add(
                        Feedback(check["type"], feedback_result, layer)
                    )

        return checked_feedback

    def check_no_absolute_filepaths(self) -> Optional[FeedbackResult]:
        if Qgis.QGIS_VERSION_INT >= 32200:
            is_absolute = self.project.filePathStorage() == Qgis.FilePathType.Absolute
        else:
            is_absolute = (
                QgsSettings().value("/qgis/defaultProjectPathsRelative") == "false"
            )

        if is_absolute:
            return FeedbackResult(
                self.tr(
                    "QField does not support projects configured to use absolute paths. "
                    'Please change this configuration in "File -> Project settings" first.'
                )
            )

    def check_no_homepath(self) -> Optional[FeedbackResult]:
        if self.project.presetHomePath():
            return FeedbackResult(
                self.tr(
                    "QField does not support projects with configured home path. "
                    'Please change this configuration in "File -> Project settings" first.'
                )
            )

    def check_basemap_configuration(self) -> Optional[FeedbackResult]:
        project_configuration = ProjectConfiguration(self.project)

        if not project_configuration.create_base_map:
            return

        base_map_type = project_configuration.base_map_type

        if base_map_type == ProjectProperties.BaseMapType.SINGLE_LAYER:
            basemap_layer = self.project.mapLayer(project_configuration.base_map_layer)

            if not project_configuration.base_map_layer.strip():
                return FeedbackResult(
                    self.tr(
                        "No basemap layer selected. "
                        'Please change this configuration in "File -> Project settings -> QField" first.'
                    )
                )

            if not basemap_layer:
                return FeedbackResult(
                    self.tr(
                        'Cannot find the configured base layer with id "{}". '
                        'Please change this configuration in "File -> Project settings -> QField" first.'
                    ).format(project_configuration.base_map_layer),
                )

        elif base_map_type == ProjectProperties.BaseMapType.MAP_THEME:
            if not self.project.mapThemeCollection().hasMapTheme(
                project_configuration.base_map_theme
            ):
                return FeedbackResult(
                    self.tr(
                        'Cannot find the configured base theme with name "{}".'
                        'Please change this configuration in "File -> Project settings -> QField" first.'
                    ).format(project_configuration.base_map_theme),
                )

    def check_files_have_unsupported_characters(self) -> Optional[FeedbackResult]:
        problematic_paths = []
        regexp = re.compile(r'[<>:"\\|?*]')
        home_path = Path(self.project.fileName()).parent
        try:
            for path in home_path.rglob("*"):
                relative_path = path.relative_to(home_path)

                if str(relative_path).startswith(".qfieldsync"):
                    continue

                if regexp.search(str(relative_path.as_posix())) is not None:
                    problematic_paths.append(relative_path)
        except FileNotFoundError:
            # long paths on windows will raise a FileNotFoundError in rglob, so we have to handle
            # that gracefully
            pass

        if problematic_paths:
            return FeedbackResult(
                self.tr(
                    'Forbidden characters in filesystem path(s) "{}". '
                    'Please make sure there are no files and directories with "<", ">", ":", "/", "\\", "|", "?", "*" or double quotes (") characters in their path.'
                ).format(", ".join([f'"{path}"' for path in problematic_paths]))
            )

    def check_project_is_dirty(self) -> Optional[FeedbackResult]:
        if self.project.isDirty():
            return FeedbackResult(
                self.tr(
                    "QGIS project has unsaved changes. "
                    "Unsaved changes will not be uploaded to QFieldCloud."
                )
            )

    def check_layer_has_utf8_datasources(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        layer = layer_source.layer

        if (
            layer.type() == QgsMapLayer.VectorLayer
            and layer.dataProvider()
            and layer.dataProvider().encoding() != "UTF-8"
            # some providers return empty string as encoding, just ignore them
            and layer.dataProvider().encoding() != ""
        ):
            return FeedbackResult(
                self.tr(
                    'Layer does not use UTF-8, but "{}" encoding. '
                    "Working with layers that do not use UTF-8 encoding might cause problems. "
                    "It is highly recommended to convert them to UTF-8 encoded layers. "
                ).format(layer.dataProvider().encoding()),
            )

    def check_layer_has_ascii_filename(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        if layer_source.is_file and not isascii(layer_source.filename):
            return FeedbackResult(
                self.tr(
                    "Non ASCII character detected in the layer filename {}. "
                    "Working with file paths that are not in ASCII might cause problems. "
                    "It is highly recommended to rename them to ASCII encoded paths. "
                ).format(layer_source.filename),
            )

    def check_layer_primary_key(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        # Do not show primary key feedback if the layer cannot be packaged
        if layer_source.package_prevention_reasons:
            return

        layer = layer_source.layer

        if layer.type() != QgsMapLayer.VectorLayer:
            return

        # when the layer is configured as "no_action" and it is an "online" layer, then QFieldCloud is not responsible for the PKs,
        # therefore we should accept them
        if (
            layer_source.cloud_action == SyncAction.NO_ACTION
            and not layer_source.is_file
        ):
            return

        try:
            layer_source.get_pk_attr_name()
        except UnsupportedPrimaryKeyError as err:
            suffix = self.tr(
                "The layer will be packaged **as a read-only layer on QFieldCloud**. "
                "Geopackages are [the recommended data format for QFieldCloud](https://docs.qfield.org/get-started/tutorials/get-started-qfc/#configure-your-project-layers-for-qfield). "
            )
            return FeedbackResult(f"{str(err)} {suffix}")

    def check_layer_memory(self, layer_source: LayerSource) -> Optional[FeedbackResult]:
        layer = layer_source.layer

        if layer.isValid() and layer.dataProvider().name() == "memory":
            return FeedbackResult(
                self.tr(
                    "Memory layer features are only available during this QGIS session. "
                    "The layer will be empty on QField."
                ),
            )

    def check_layer_configured(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        if not layer_source.is_configured and not layer_source.is_cloud_configured:
            return FeedbackResult(
                self.tr(
                    "The layer is not configured with neither cable, nor cloud action yet. "
                    "Default action will be selected only for this time. "
                    'Please select and save appropriate layer action in "Layer Properties -> QField". '
                ),
            )

    def check_layer_package_prevention(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        package_prevention_reasons = layer_source.package_prevention_reasons
        if package_prevention_reasons:
            # remove the layer if it is invalid or not supported datasource on QField
            main_msg = ""
            reason_msgs = []
            for reason in package_prevention_reasons:
                if reason in LayerSource.REASONS_TO_REMOVE_LAYER:
                    main_msg = self.tr(
                        "The layer will be removed from the packaged project."
                    )
                else:
                    main_msg = self.tr("The layer's data will not be packaged!")

                if reason == LayerSource.PackagePreventionReason.INVALID:
                    reason_msgs.append(self.tr("The layer is invalid!"))
                elif (
                    reason == LayerSource.PackagePreventionReason.UNSUPPORTED_DATASOURCE
                ):
                    reason_msgs.append(
                        self.tr("The layer data source is not supported on QField!")
                    )
                elif reason == LayerSource.PackagePreventionReason.LOCALIZED_PATH:
                    reason_msgs.append(self.tr("The layer is localized data path!"))

            main_msg += "\n\n"
            main_msg += "\n".join(f"- {r}" for r in reason_msgs)

            return FeedbackResult(main_msg)
