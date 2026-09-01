from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, TypedDict

from qgis.core import Qgis, QgsMapLayer, QgsProject
from qgis.PyQt.QtCore import QObject

from libqfieldsync.layer import LayerSource, SyncAction, UnsupportedPrimaryKeyError
from libqfieldsync.project import BaseMapType, ProjectConfig
from libqfieldsync.utils.file_utils import is_valid_filepath, isascii

from .offline_converter import ExportType


class FeedbackTypeId(Enum):
    ABSOLUTE_FILEPATHS = "absolute_filepaths"
    HOME_PATH = "home_path"
    UNSUPPORTED_CHARACTERS = "unsupported_characters"
    PROJECT_IS_DIRTY = "project_is_dirty"
    CONFLICTING_BASE_FILENAMES = "conflicting_base_filenames"
    BASEMAP_CONFIGURATION = "basemap_configuration"
    LAYER_ENCODING = "layer_encoding"
    LAYER_FILENAME_ASCII = "layer_filename_ascii"
    LAYER_EXTERNAL = "layer_external"
    LAYER_PRIMARY_KEY = "layer_primary_key"
    LAYER_MEMORY = "layer_memory"
    LAYER_CONFIGURED = "layer_configured"
    LAYER_PACKAGE_PREVENTION = "layer_package_prevention"
    EXPERIMENTAL_CLOUD = "experimental_cloud"
    CONFLICTING_LAYER_ACTIONS = "conflicting_layer_actions"


class FeedbackResult:
    def __init__(
        self,
        type_id: FeedbackTypeId,
        message: str,
    ) -> None:
        self.type_id = type_id
        self.message = message


class Feedback:
    class Level(Enum):
        ERROR = "ERROR"
        WARNING = "WARNING"

    def __init__(
        self,
        level: Level,
        feedback_result: FeedbackResult,
        layer: Optional[QgsMapLayer] = None,
    ) -> None:
        self.level = level
        self.type_id = feedback_result.type_id
        self.message = feedback_result.message
        if layer:
            self.layer_id = layer.id()
            self.layer_name = layer.name()
        else:
            self.layer_id = None
            self.layer_name = None


class ProjectCheckerFeedback:
    tr = QObject().tr

    def __init__(self) -> None:
        self.feedbacks: dict[str, list[Feedback]] = {
            # if the key is "", it is considered as project feedback
            "": [],
        }
        self.count = 0
        self.error_feedbacks: list[Feedback] = []
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
        level: Feedback.Level
        fn: Callable
        scope: Optional[ExportType]

    def __init__(self, project: QgsProject) -> None:
        self.project = project
        self.project_checks: list[ProjectChecker.CheckConfig] = [
            {
                "level": Feedback.Level.ERROR,
                "fn": self.check_no_absolute_filepaths,
                "scope": None,
            },
            {
                "level": Feedback.Level.ERROR,
                "fn": self.check_no_homepath,
                "scope": None,
            },
            {
                "level": Feedback.Level.ERROR,
                "fn": self.check_files_have_unsupported_characters,
                "scope": None,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_project_is_dirty,
                "scope": None,
            },
            {
                "level": Feedback.Level.ERROR,
                "fn": self.check_project_layers_sources_actions,
                "scope": ExportType.Cable,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_for_conflicting_base_filenames,
                "scope": ExportType.Cloud,
            },
            {
                "level": Feedback.Level.ERROR,
                "fn": self.check_basemap_configuration,
                "scope": None,
            },
        ]
        self.layer_checks: list[ProjectChecker.CheckConfig] = [
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_layer_has_utf8_datasources,
                "scope": None,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_layer_has_ascii_filename,
                "scope": None,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_external_layers,
                "scope": None,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_layer_primary_key,
                "scope": ExportType.Cloud,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_layer_memory,
                "scope": None,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_layer_configured,
                "scope": None,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_layer_package_prevention,
                "scope": None,
            },
            {
                "level": Feedback.Level.WARNING,
                "fn": self.check_layer_has_experimental_cloud_support,
                "scope": ExportType.Cloud,
            },
        ]

    def check(self, scope: ExportType) -> ProjectCheckerFeedback:
        checked_feedback = ProjectCheckerFeedback()

        for check in self.project_checks:
            if check["scope"] and check["scope"] != scope:
                continue

            feedback_result = check["fn"]()
            if feedback_result:
                checked_feedback.add(Feedback(check["level"], feedback_result))

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
                        Feedback(check["level"], feedback_result, layer)
                    )

        return checked_feedback

    def check_no_absolute_filepaths(self) -> Optional[FeedbackResult]:
        if self.project.filePathStorage() == Qgis.FilePathType.Absolute:
            return FeedbackResult(
                FeedbackTypeId.ABSOLUTE_FILEPATHS,
                self.tr(
                    "QField does not support projects configured to use absolute paths. "
                    'Please change this configuration in "File -> Project settings" first.'
                ),
            )
        else:
            return None

    def check_no_homepath(self) -> Optional[FeedbackResult]:
        if self.project.presetHomePath():
            return FeedbackResult(
                FeedbackTypeId.HOME_PATH,
                self.tr(
                    "QField does not support projects with configured home path. "
                    'Please change this configuration in "File -> Project settings" first.'
                ),
            )
        else:
            return None

    def check_basemap_configuration(self) -> Optional[FeedbackResult]:
        project_config = ProjectConfig(self.project)

        if not project_config.create_base_map:
            return None

        base_map_type = project_config.base_map_type

        if base_map_type == BaseMapType.SINGLE_LAYER:
            basemap_layer = self.project.mapLayer(project_config.base_map_layer)

            if not project_config.base_map_layer.strip():
                return FeedbackResult(
                    FeedbackTypeId.BASEMAP_CONFIGURATION,
                    self.tr(
                        "No basemap layer selected. "
                        'Please change this configuration in "Project -> Properties... -> QField" first.'
                    ),
                )
            elif not basemap_layer:
                return FeedbackResult(
                    FeedbackTypeId.BASEMAP_CONFIGURATION,
                    self.tr(
                        'Cannot find the configured base layer with id "{}". '
                        'Please change this configuration in "Project -> Properties... -> QField" first.'
                    ).format(project_config.base_map_layer),
                )

        elif base_map_type == BaseMapType.MAP_THEME:
            if not self.project.mapThemeCollection().hasMapTheme(
                project_config.base_map_theme
            ):
                return FeedbackResult(
                    FeedbackTypeId.BASEMAP_CONFIGURATION,
                    self.tr(
                        'Cannot find the configured base theme with name "{}".'
                        'Please change this configuration in "Project -> Properties... -> QField" first.'
                    ).format(project_config.base_map_theme),
                )

        return None

    def check_files_have_unsupported_characters(self) -> Optional[FeedbackResult]:
        problematic_paths = []
        home_path = Path(self.project.fileName()).parent
        try:
            for path in home_path.rglob("*"):
                relative_path = path.relative_to(home_path)

                if str(relative_path).startswith(".qfieldsync"):
                    continue

                if not is_valid_filepath(str(relative_path.as_posix())):
                    problematic_paths.append(relative_path)

        except FileNotFoundError:
            # long paths on windows will raise a FileNotFoundError in rglob, so we have to handle
            # that gracefully
            pass

        if problematic_paths:
            return FeedbackResult(
                FeedbackTypeId.UNSUPPORTED_CHARACTERS,
                self.tr(
                    'Forbidden characters in filesystem path(s) "{}". '
                    'Please make sure there are no files and directories with "<", ">", ":", "/", "\\", "|", "?", "*" or double quotes (") characters in their path.'
                    "and must not be reserved names like CON, PRN, AUX, NUL, etc."
                ).format(", ".join([f'"{path}"' for path in problematic_paths])),
            )
        else:
            return None

    def check_project_is_dirty(self) -> Optional[FeedbackResult]:
        if self.project.isDirty():
            return FeedbackResult(
                FeedbackTypeId.PROJECT_IS_DIRTY,
                self.tr(
                    "QGIS project has unsaved changes. "
                    "Unsaved changes will not be included on the project."
                ),
            )
        else:
            return None

    def check_for_conflicting_base_filenames(self) -> Optional[FeedbackResult]:
        conflicting_files: list[Path] = []

        project_file_path = Path(self.project.fileName())
        project_home_path = project_file_path.parent
        project_base_name = project_file_path.stem

        for item in project_home_path.rglob("*"):
            if (
                item.is_file()
                and item != project_file_path
                and item.stem == project_base_name
                and not item.name.endswith("~")
                and ".qfieldsync" not in item.relative_to(project_home_path).parts
            ):
                conflicting_files.append(item)

        if conflicting_files:
            return FeedbackResult(
                FeedbackTypeId.CONFLICTING_BASE_FILENAMES,
                self.tr(
                    'The project "{}" shares its base file name ({}) with the following files, which might cause issues: {}.'
                ).format(
                    project_base_name,
                    project_file_path.name,
                    ", ".join([f'"{path}"' for path in conflicting_files]),
                ),
            )

        return None

    def check_layer_has_utf8_datasources(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        layer = layer_source.layer

        if (
            layer.type() == QgsMapLayer.LayerType.VectorLayer
            and layer.dataProvider()
            and layer.dataProvider().encoding() != "UTF-8"
            # some providers return empty string as encoding, just ignore them
            and layer.dataProvider().encoding() != ""
        ):
            return FeedbackResult(
                FeedbackTypeId.LAYER_ENCODING,
                self.tr(
                    'Layer does not use UTF-8, but "{}" encoding. '
                    "Working with layers that do not use UTF-8 encoding might cause problems. "
                    "It is highly recommended to convert them to UTF-8 encoded layers. "
                ).format(layer.dataProvider().encoding()),
            )
        else:
            return None

    def check_layer_has_ascii_filename(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        if layer_source.is_file and not isascii(layer_source.filename):
            return FeedbackResult(
                FeedbackTypeId.LAYER_FILENAME_ASCII,
                self.tr(
                    "Non ASCII character detected in the layer filename {}. "
                    "Working with file paths that are not in ASCII might cause problems. "
                    "It is highly recommended to rename them to ASCII encoded paths. "
                ).format(layer_source.filename),
            )
        else:
            return None

    def check_layer_primary_key(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        # Do not show primary key feedback if the layer cannot be packaged
        if layer_source.package_prevention_reasons:
            return None

        layer = layer_source.layer

        if layer.type() != QgsMapLayer.LayerType.VectorLayer:
            return None

        # when the layer is configured as "no_action" and it is an "online" layer, then QFieldCloud is not responsible for the PKs,
        # therefore we should accept them
        if (
            layer_source.cloud_action == SyncAction.NO_ACTION
            and not layer_source.is_file
        ):
            return None

        if not layer.readOnly():
            try:
                layer_source.get_pk_attr_name()
            except UnsupportedPrimaryKeyError as err:
                suffix = self.tr(
                    "The layer will be packaged **as a read-only layer on QFieldCloud**. "
                    "Geopackages are [the recommended data format for QFieldCloud](https://docs.qfield.org/get-started/tutorials/get-started-qfc/#configure-your-project-layers-for-qfield). "
                )
                return FeedbackResult(
                    FeedbackTypeId.LAYER_PRIMARY_KEY, f"{err!s} {suffix}"
                )

        return None

    def check_layer_memory(self, layer_source: LayerSource) -> Optional[FeedbackResult]:
        layer = layer_source.layer

        if layer.isValid() and layer.dataProvider().name() == "memory":
            return FeedbackResult(
                FeedbackTypeId.LAYER_MEMORY,
                self.tr(
                    "Memory layer features are only available during this QGIS session. "
                    "The layer will be empty on QField."
                ),
            )

        return None

    def check_layer_configured(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        if not layer_source.is_configured and not layer_source.is_cloud_configured:
            return FeedbackResult(
                FeedbackTypeId.LAYER_CONFIGURED,
                self.tr(
                    "The layer is not configured with neither cable, nor cloud action yet. "
                    "Default action will be selected only for this time. "
                    'Please select and save appropriate layer action in "Layer Properties -> QField". '
                ),
            )

        return None

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
                    reason
                    == LayerSource.PackagePreventionReason.INVALID_REMOTE_RASTER_LAYER
                ):
                    reason_msgs.append(
                        self.tr(
                            "The raster layer data source is not accessible from the current network!"
                        )
                    )
                elif (
                    reason == LayerSource.PackagePreventionReason.UNSUPPORTED_DATASOURCE
                ):
                    reason_msgs.append(
                        self.tr("The layer data source is not supported on QField!")
                    )
                elif reason == LayerSource.PackagePreventionReason.LOCALIZED_PATH:
                    reason_msgs.append(self.tr("The layer is a shared dateset path!"))

            main_msg += "\n\n"
            main_msg += "\n".join(f"- {r}" for r in reason_msgs)

            return FeedbackResult(FeedbackTypeId.LAYER_PACKAGE_PREVENTION, main_msg)

        return None

    def check_external_layers(
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        if (
            not layer_source.is_file
            or not layer_source.filename
            or layer_source.is_localized_path
        ):
            return None

        home_path = Path(self.project.fileName()).parent.resolve()
        layer_path = Path(layer_source.filename).resolve()

        if home_path and home_path not in layer_path.parents:
            return FeedbackResult(
                FeedbackTypeId.LAYER_EXTERNAL,
                self.tr(
                    'Layer "{}" is outside the project\'s home directory. '
                    "QFieldSync may not transfer your layer. "
                    'Please move the file to "{}".'
                ).format(layer_source.filename, home_path),
            )

        return None

    def check_layer_has_experimental_cloud_support(  # noqa: PLR0911
        self, layer_source: LayerSource
    ) -> Optional[FeedbackResult]:
        """Check if layer has experimental cloud support"""
        layer = layer_source.layer

        if layer_source.cloud_action != SyncAction.OFFLINE:
            return None

        if layer.readOnly():
            return None

        if not layer.dataProvider():
            return None

        provider_type = layer.providerType()
        storage_type = layer.dataProvider().storageType()

        # We support GeoPackages on the cloud, so return early
        if storage_type == "GPKG":
            return None

        if provider_type == "postgres":
            return None

        if (
            provider_type == "delimitedtext" and storage_type == "Delimited text file"
        ) or (provider_type == "ogr" and storage_type == "CSV"):
            return None

        return FeedbackResult(
            FeedbackTypeId.EXPERIMENTAL_CLOUD,
            self.tr(
                'The datasource type "{}" '
                "has experimental support on QFieldCloud. "
                "Consider converting your data to the officially supported "
                "GeoPackage or PostGIS datasources."
            ).format(storage_type),
        )

    def check_project_layers_sources_actions(self) -> Optional[FeedbackResult]:
        """Check if layers from the same GeoPackage have offline and copy actions."""
        layer_sources_by_filename: dict[str, list[LayerSource]] = defaultdict(list)

        for project_layer in self.project.mapLayers().values():
            layer_source = LayerSource(project_layer)

            if not layer_source.is_file:
                continue

            if layer_source.action not in [SyncAction.OFFLINE, SyncAction.COPY]:
                continue

            layer_sources_by_filename[layer_source.filename].append(layer_source)

            # Check for layers with mixed actions
            for filename, layer_sources in layer_sources_by_filename.items():
                assert layer_sources

                has_mixed_actions = False
                first_layer_source_action = layer_sources[0].action

                for layer_source in layer_sources:
                    if layer_source.action != first_layer_source_action:
                        has_mixed_actions = True
                        break

                if not has_mixed_actions:
                    continue

                message = self.tr(
                    "Layers having the same file datasource have conflicting sync actions that may cause data loss.\n"
                    'Layers {} share the same file "{}".'
                ).format(
                    ", ".join(f'"{layer.name}"' for layer in layer_sources),
                    filename,
                )
                return FeedbackResult(FeedbackTypeId.CONFLICTING_LAYER_ACTIONS, message)

        return None
