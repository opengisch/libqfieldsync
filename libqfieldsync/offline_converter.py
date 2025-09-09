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
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, TypedDict, Union, cast

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsEditorWidgetSetup,
    QgsField,
    QgsFields,
    QgsLayerTreeGroup,
    QgsLayerTreeModel,
    QgsMapLayer,
    QgsMapThemeCollection,
    QgsPolygon,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsValueRelationFieldFormatter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QObject, pyqtSignal

from .layer import LayerSource, SyncAction
from .offliners import BaseOffliner
from .project import ProjectConfiguration, ProjectProperties
from .utils.file_utils import (
    copy_attachments,
    set_relative_embed_layer_symbols_on_project,
)
from .utils.logger import logger
from .utils.qgis import make_temp_qgis_file, open_project
from .utils.xml import get_themapcanvas

FID_NULL = -4294967296


class LayerData(TypedDict):
    id: str
    name: str
    source: str
    type: int
    fields: Optional[QgsFields]


class PackagingError(Exception):
    pass


class PackagingCanceledError(PackagingError):
    """Exception to be raised when offline converting is canceled"""

    def __init__(self, *args):
        super().__init__(QObject().tr("Packaging canceled by the user"), *args)


class PackagingFailedError(PackagingError):
    def __init__(self, *args):
        super().__init__(*args)


class ExportType(Enum):
    Cable = "cable"
    Cloud = "cloud"


class OfflineConverter(QObject):
    progress_stopped = pyqtSignal()
    warning = pyqtSignal(str, str)
    task_progress_updated = pyqtSignal(int, int)
    total_progress_updated = pyqtSignal(int, int, str)

    # feedback used for basemap generation processing algorithm
    _feedback = QgsProcessingFeedback()

    _is_canceled: bool = False

    def __init__(  # noqa: PLR0913
        self,
        project: QgsProject,
        export_filename: str,
        area_of_interest_wkt: str,
        area_of_interest_crs: Union[str, QgsCoordinateReferenceSystem],
        attachment_dirs: List[str],
        offliner: BaseOffliner,
        export_type: Optional[ExportType] = None,
        create_basemap: bool = True,
        dirs_to_copy: Optional[Dict[str, bool]] = None,
        export_title: str = "",
    ) -> None:
        # NOTE: while it will be much more readable to pass `export_type` parameter to have a default value of `ExportType.Cable`,
        # we cannot reference an Enum as a default value in a QGIS plugin, as when the file reloaded (e.g. enable/disable the plugin, Plugin reloader),
        # the enum will have a different address and comparisons will fail.
        if export_type is None:
            export_type = ExportType.Cable

        super().__init__(parent=None)
        self.__max_task_progress = 0
        self.__convertor_progress = None  # for processing feedback
        self.__layer_data_by_id: Dict[str, LayerData] = {}
        self.__offline_layer_names: List[str] = []

        # elipsis workaround
        self.trUtf8 = self.tr

        if not export_filename:
            raise PackagingFailedError(self.tr("Empty export filename provided!"))

        self._export_filename = Path(export_filename)
        self._export_title = export_title
        self.export_type = export_type
        self.create_basemap = create_basemap
        self.area_of_interest = QgsPolygon()
        self.area_of_interest.fromWkt(area_of_interest_wkt)
        self.area_of_interest_crs = QgsCoordinateReferenceSystem(area_of_interest_crs)
        self.attachment_dirs = attachment_dirs
        self.dirs_to_copy = dirs_to_copy

        self.offliner = offliner

        self.offliner.layer_progress_updated.connect(
            self._on_offline_editing_next_layer
        )
        self.offliner.progress_mode_set.connect(self._on_offline_editing_max_changed)
        self.offliner.progress_updated.connect(self._on_offline_editing_task_progress)

        self.project_configuration = ProjectConfiguration(project)

        if (
            self.project_configuration.offline_copy_only_aoi
            or self.project_configuration.create_base_map
        ):
            assert self.area_of_interest.isValid()[0]
            assert self.area_of_interest_crs.isValid(), (
                f"Invalid CRS specified for area of interest {area_of_interest_crs}"
            )

    def convert(self, reload_original_project: bool = True) -> None:
        """Convert the project to a portable project."""
        project = QgsProject.instance()
        self.original_filename = Path(project.fileName())
        self.backup_filename = make_temp_qgis_file(project)

        try:
            self._convert(project)
        finally:
            if reload_original_project:
                QCoreApplication.processEvents()
                QgsProject.instance().clear()
                QCoreApplication.processEvents()

                open_project(str(self.original_filename), self.backup_filename)

            self.total_progress_updated.emit(100, 100, self.tr("Finished"))

    def _get_additional_project_files(self, project: QgsProject) -> List[str]:
        original_project_path = Path(self.original_filename).parent
        additional_project_files = []

        # Check for image decoration asset
        image_decoration_path, _ = project.readEntry("Image", "/ImagePath", "")
        if image_decoration_path:
            image_decoration_path = Path(image_decoration_path)
            if not image_decoration_path.is_absolute():
                image_decoration_path = original_project_path.joinpath(
                    image_decoration_path
                ).resolve()

            if image_decoration_path.is_file() and image_decoration_path.is_relative_to(
                original_project_path
            ):
                additional_project_files.append(str(image_decoration_path))

        # Check for project plugin asset
        plugin_file = Path("{}.qml".format(str(self.original_filename)[:-4]))  # noqa: UP032
        if plugin_file.exists():
            additional_project_files.append(str(plugin_file))

        return additional_project_files

    def _convert(self, project: QgsProject) -> None:  # noqa: PLR0912, PLR0915
        xml_elements_to_preserve = {}
        tmp_project_filename = ""

        if self.export_type == ExportType.Cable:
            # the `backup_filename` is copied right after packaging is requested. It has all the unsaved
            # project settings, which means they will be available in the packaged project too.
            tmp_project_filename = self.backup_filename
        elif self.export_type == ExportType.Cloud:
            # if you save the project without QGIS GUI, the project no longer has `theMapCanvas` canvas
            # so we should use the original project file that already has `theMapCanvas`. There is no
            # gain using the `backup_filename`, since there is no user to modify the project.
            tmp_project_filename = project.fileName()
        else:
            raise NotImplementedError(f"Unknown package type: {self.export_type}")

        # Set flags that usually significantly speed-up project file read
        read_flags = QgsProject.ReadFlags()
        read_flags |= QgsProject.FlagDontResolveLayers
        read_flags |= QgsProject.FlagDontLoadLayouts
        if Qgis.versionInt() >= 32600:  # noqa: PLR2004
            read_flags |= QgsProject.FlagDontLoad3DViews

        # Make a new function object that we can connect and disconnect easily
        on_original_project_read = self._on_original_project_read_wrapper(
            xml_elements_to_preserve
        )

        # Create a new temporary `QgsProject` instance just to make sure that `theMapCanvas`
        # XML object is properly set within the XML document. Using a new `QgsProject`
        # instead of the singleton `QgsProject.instance()` allows using the read flags.
        tmp_project = QgsProject()
        tmp_project.readProject.connect(on_original_project_read)
        tmp_project.read(tmp_project_filename, read_flags)
        tmp_project.readProject.disconnect(on_original_project_read)

        # Additional project files, such as image decoration, to be copied to the converted project
        additional_project_files = self._get_additional_project_files(project)

        # NOTE force delete the `QgsProject`, otherwise the `QgsApplication` might be deleted by the time the project is garbage collected
        del tmp_project

        self._export_filename.parent.mkdir(parents=True, exist_ok=True)
        self.total_progress_updated.emit(0, 100, self.trUtf8("Converting project…"))

        project_layers: List[QgsMapLayer] = list(project.mapLayers().values())
        offline_layers: List[QgsMapLayer] = []
        copied_files = []

        if self.create_basemap and self.project_configuration.create_base_map:
            is_basemap_export_success = self._export_basemap()

            if not is_basemap_export_success and not self._is_canceled:
                self.warning.emit(
                    self.tr("Failed to create basemap"),
                    self.tr("The basemap creation was unsuccessful."),
                )

        # We store the pks of the original vector layers
        for layer_idx, layer in enumerate(project_layers):
            layer_source = LayerSource(layer)

            # NOTE if the layer is prevented from packaging it does NOT mean we have to remove it, but we cannot collect any layer metadata (e.g. if the layer is localized path).
            # NOTE cache the value, since we might remove the layer and the reasons cannot be recalculated
            package_prevention_reasons = layer_source.package_prevention_reasons
            if package_prevention_reasons:
                # remove the layer if it is invalid or not supported datasource on QField
                for reason in package_prevention_reasons:
                    if reason in LayerSource.REASONS_TO_REMOVE_LAYER:
                        logger.warning(
                            f'Layer "{layer.name()}" cannot be packaged and will be removed because "{reason}".'
                        )

                        project.removeMapLayer(layer)

                        break

                    logger.warning(
                        f'Layer "{layer.name()}" cannot be packaged due to "{reason}", skipping…'
                    )

                # do not attempt to package the layer
                continue

            layer_data: LayerData = {
                "id": layer.id(),
                "name": layer.name(),
                "type": layer.type(),
                "source": layer.source(),
                "fields": layer.fields() if hasattr(layer, "fields") else None,
            }

            layer_action = (
                layer_source.action
                if self.export_type == ExportType.Cable
                else layer_source.cloud_action
            )

            if layer.isValid() and layer.type() == QgsMapLayer.VectorLayer:
                if layer_source.pk_attr_name:
                    # NOTE even though `QFieldSync/sourceDataPrimaryKeys` is in plural, we never supported composite (multi-column) PKs and always stored a single value
                    layer.setCustomProperty(
                        "QFieldSync/sourceDataPrimaryKeys", layer_source.pk_attr_name
                    )
                else:
                    # The layer has no supported PK, so we mark it as readonly and just copy it when packaging in the cloud
                    if self.export_type == ExportType.Cloud:
                        layer_action = SyncAction.NO_ACTION
                        layer.setReadOnly(True)
                        layer.setCustomProperty("QFieldSync/unsupported_source_pk", "1")

            self.__layer_data_by_id[layer.id()] = layer_data

            # `QFieldSync/remoteLayerId` should be equal to `remoteLayerId`, which is already set by `QgsOfflineEditing`. We add this as a copy to have control over this attribute that might suddenly change on QGIS.
            layer.setCustomProperty("QFieldSync/remoteLayerId", layer.id())

            self.total_progress_updated.emit(
                layer_idx,
                len(project_layers),
                self.trUtf8("Copying layers…"),
            )

            self._check_canceled()

            if layer_action == SyncAction.OFFLINE:
                offline_layers.append(layer)
                self.__offline_layer_names.append(layer.name())
            elif layer_action in (SyncAction.COPY, SyncAction.NO_ACTION):
                copied_files = layer_source.copy(
                    self._export_filename.parent,
                    copied_files,
                )
            elif layer_action == SyncAction.KEEP_EXISTENT:
                layer_source.copy(self._export_filename.parent, copied_files, True)
            elif layer_action == SyncAction.REMOVE:
                project.removeMapLayer(layer)

        self.remove_empty_groups_from_layer_tree_group(project.layerTreeRoot())

        export_project_filename = self._export_filename

        # save the original project path
        self.project_configuration.original_project_path = str(self.original_filename)

        self._check_canceled()

        # copy additional project files (e.g. layout images, symbology images, etc)
        if additional_project_files:
            project_path = Path(project.fileName()).parent
            for additional_project_file in additional_project_files:
                additional_project_file_path = Path(additional_project_file)
                relative_path = additional_project_file_path.relative_to(project_path)
                destination_file = self._export_filename.parent.joinpath(
                    relative_path
                ).resolve()
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(additional_project_file, str(destination_file))

        # save the offline project twice so that the offline plugin can "know" that it's a relative path
        QgsProject.instance().write(str(export_project_filename))

        if self.dirs_to_copy is None:
            dirs_to_copy = {}
            for d in self.attachment_dirs:
                dirs_to_copy[d] = True
        else:
            dirs_to_copy = self.dirs_to_copy

        for source_dir, should_copy in dirs_to_copy.items():
            if not should_copy:
                continue

            self._check_canceled()

            copy_attachments(
                self.original_filename.parent,
                export_project_filename.parent,
                Path(source_dir),
            )

        if offline_layers:
            bbox = None
            if self.project_configuration.offline_copy_only_aoi:
                bbox = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem(self.area_of_interest_crs),
                    QgsProject.instance().crs(),
                    QgsProject.instance(),
                ).transformBoundingBox(self.area_of_interest.boundingBox())

            self._check_canceled()

            is_success = self.offliner.convert_to_offline(
                str(self._export_filename.with_name("data.gpkg")),
                offline_layers,
                bbox,
                self._export_title,
            )

            if not is_success:
                raise PackagingFailedError(
                    self.tr(
                        "QGIS Offline editing error: failed to convert layers to offline layers"
                    )
                )

            self._check_canceled()

            # Disable project options that could create problems on a portable
            # project with offline layers
            self.post_process_offline_layers()
        # Change SVG and Raster symbols path to relative or embedded
        for layer in QgsProject.instance().mapLayers().values():
            set_relative_embed_layer_symbols_on_project(
                layer, self.original_filename.parent, self._export_filename.parent
            )

        self._check_canceled()

        # Now we have a project state which can be saved as offline project
        on_original_project_write = self._on_original_project_write_wrapper(
            xml_elements_to_preserve
        )
        project.writeProject.connect(on_original_project_write)
        QgsProject.instance().write(str(export_project_filename))
        project.writeProject.disconnect(on_original_project_write)

    def remove_empty_groups_from_layer_tree_group(
        self, group: QgsLayerTreeGroup
    ) -> None:
        """Recursively removes any empty groups from the given layer tree group."""
        for child in group.children():
            if not isinstance(child, QgsLayerTreeGroup):
                continue

            # remove recursively
            self.remove_empty_groups_from_layer_tree_group(child)

            if not child.children():
                group.removeChildNode(child)

    def post_process_offline_layers(self):
        project = QgsProject.instance()

        if Qgis.versionInt() >= 34000:  # noqa: PLR2004
            project.setFlag(Qgis.ProjectFlag.EvaluateDefaultValuesOnProviderSide, False)
        else:
            project.setEvaluateDefaultValues(False)

        if Qgis.versionInt() >= 32600:  # noqa: PLR2004
            project.setTransactionMode(Qgis.TransactionMode.Disabled)
        else:
            project.setAutoTransaction(False)

        # check if value relations point to offline layers and adjust if necessary
        for e_layer in project.mapLayers().values():
            if e_layer.type() == QgsMapLayer.VectorLayer:
                remote_layer_id = e_layer.customProperty("QFieldSync/remoteLayerId")
                if (
                    not remote_layer_id
                    or remote_layer_id not in self.__layer_data_by_id
                ):
                    self.warning.emit(
                        self.tr("QFieldSync"),
                        self.tr(
                            'Failed to find layer with name "{}". QFieldSync will not package that layer.'
                        ).format(e_layer.name()),
                    )
                    continue

                self.post_process_fields(e_layer)

    def post_process_fields(self, e_layer: QgsVectorLayer) -> None:
        remote_layer_id = e_layer.customProperty("QFieldSync/remoteLayerId")
        e_layer_source = LayerSource(e_layer)
        o_layer_data = self.__layer_data_by_id[remote_layer_id]
        # since we have vector layer, then the fields must be available
        o_layer_fields: QgsFields = cast("QgsFields", o_layer_data["fields"])
        o_layer_field_names = o_layer_fields.names()

        for e_field_name in e_layer_source.visible_fields_names():
            if e_field_name not in o_layer_field_names:
                # handles the `fid` column, that is present only for gpkg
                e_layer.setEditorWidgetSetup(
                    e_layer.fields().indexFromName(e_field_name),
                    QgsEditorWidgetSetup("Hidden", {}),
                )
                continue

            o_field = o_layer_fields.field(e_field_name)
            o_ews = o_field.editorWidgetSetup()

            if o_ews.type() == "ValueRelation":
                self.post_process_value_relation_fields(e_layer, o_field)

    def post_process_value_relation_fields(
        self, e_layer: QgsVectorLayer, o_field: QgsField
    ):
        project = QgsProject.instance()
        o_ews = o_field.editorWidgetSetup()
        o_widget_config = o_ews.config()
        o_referenced_layer_id = o_widget_config["Layer"]

        if o_referenced_layer_id not in self.__layer_data_by_id:
            e_referenced_layer = QgsValueRelationFieldFormatter.resolveLayer(
                o_widget_config, project
            )

            if e_referenced_layer:
                o_referenced_layer_id = e_referenced_layer.customProperty(
                    "remoteLayerId"
                )

        # yet another check whether value relation resolver succeeded
        if o_referenced_layer_id not in self.__layer_data_by_id:
            self.warning.emit(
                self.tr("Bad attribute form configuration"),
                self.tr(
                    'Field "{}" in layer "{}" has no configured layer in the value relation widget.'
                ).format(o_field.name(), e_layer.name()),
            )
            return

        e_referenced_layer_id = None
        for layer in project.mapLayers().values():
            o_layer_data = self.__layer_data_by_id[o_referenced_layer_id]

            if layer.customProperty("remoteSource") == o_layer_data["source"]:
                #  First try strict matching: the offline layer should have a "remoteSource" property
                e_referenced_layer_id = layer.id()
                break

            if layer.name() == o_layer_data["name"]:
                #  If that did not work, go with loose matching
                e_referenced_layer_id = layer.id()
                break

        if not e_referenced_layer_id:
            self.warning.emit(
                self.tr("Bad attribute form configuration"),
                self.tr(
                    'Field "{}" in layer "{}" has no configured layer in the value relation widget.'
                ).format(o_field.name(), e_layer.name()),
            )
            return

        e_widget_config = o_widget_config
        e_widget_config["Layer"] = e_referenced_layer_id
        e_layer.setEditorWidgetSetup(
            e_layer.fields().indexOf(o_field.name()),
            QgsEditorWidgetSetup(o_ews.type(), e_widget_config),
        )

    def _export_basemap_requirements_check(self) -> bool:
        try:
            # NOTE if qgis is built without GUI, there is no `qgis.utils`, since it depends on `qgis.gui`
            import qgis.utils  # noqa: PLC0415

            if "processing" in qgis.utils.plugins:
                return True

            self.warning.emit(
                self.tr('QFieldSync requires "processing" plugin'),
                self.tr(
                    "Creating a basemap with QFieldSync requires the processing plugin to be enabled. Processing is not enabled on your system. Please go to Plugins > Manage and Install Plugins and enable processing."
                ),
            )
            self.total_progress_updated.emit(0, 0, self.trUtf8("Cancelled"))
        except Exception:
            logger.warning(
                "Creating a basemap with QFieldSync requires the processing plugin to be enabled. Processing is not enabled on your system. Please go to Plugins > Manage and Install Plugins and enable processing."
            )

        return False

    def _export_basemap(self) -> bool:
        self.total_progress_updated.emit(0, 1, self.trUtf8("Creating base map…"))

        if not self._export_basemap_requirements_check():
            return False

        project = QgsProject.instance()
        basemap_extent = self.area_of_interest.boundingBox()

        if basemap_extent.isNull() or not basemap_extent.isFinite():
            self.warning.emit(
                self.tr("Failed to create basemap"),
                self.tr("Cannot create basemap for the given area of interest."),
            )
            return False

        extent = basemap_extent
        base_map_type = self.project_configuration.base_map_type
        if base_map_type == ProjectProperties.BaseMapType.SINGLE_LAYER:
            if not self.project_configuration.base_map_layer.strip():
                self.warning.emit(
                    self.tr("Failed to create basemap"),
                    self.tr(
                        "No basemap layer selected. Please check the project configuration."
                    ),
                )
                return False

            basemap_layer = project.mapLayer(self.project_configuration.base_map_layer)

            if not basemap_layer:
                self.warning.emit(
                    self.tr("Failed to create basemap"),
                    self.tr(
                        'Cannot find the configured base layer with id "{}". Please check the project configuration.'
                    ).format(self.project_configuration.base_map_layer),
                )
                return False

            # we need to transform the extent to match the one of the selected layer
            extent = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem(self.area_of_interest_crs),
                project.crs(),
                project,
            ).transformBoundingBox(basemap_extent)
        elif base_map_type == ProjectProperties.BaseMapType.MAP_THEME:
            if not project.mapThemeCollection().hasMapTheme(
                self.project_configuration.base_map_theme
            ):
                self.warning.emit(
                    self.tr("Failed to create basemap"),
                    self.tr(
                        'Cannot find the configured base theme with name "{}". Please check the project configuration.'
                    ).format(self.project_configuration.base_map_theme),
                )
                return False

        exported_mbtiles = self._export_basemap_as_mbtiles(extent, base_map_type)

        return exported_mbtiles

    def _export_basemap_as_mbtiles(
        self, extent: QgsRectangle, base_map_type: ProjectProperties.BaseMapType
    ) -> bool:
        """
        Exports a basemap to mbtiles format.
        This method handles several zoom levels.

        Args:
            extent (QgsRectangle): extent of the area of interest
            base_map_type (ProjectProperties.BaseMapType): basemap type (layer or theme)

        Returns:
            bool: if basemap layer could be exported as mbtiles

        """
        alg = (
            QgsApplication.instance()
            .processingRegistry()
            .createAlgorithmById("native:tilesxyzmbtiles")
        )

        basemap_export_path = self._export_filename.with_name("basemap.mbtiles")

        params = {
            "EXTENT": extent,
            "ZOOM_MIN": self.project_configuration.base_map_tiles_min_zoom_level,
            "ZOOM_MAX": self.project_configuration.base_map_tiles_max_zoom_level,
            "TILE_SIZE": self.project_configuration.base_map_tile_size,
            "OUTPUT_FILE": str(basemap_export_path),
        }

        # clone current QGIS project
        current_project = QgsProject.instance()
        cloned_project = QgsProject(
            parent=current_project.parent(), capabilities=current_project.capabilities()
        )
        cloned_project.setCrs(current_project.crs())

        if base_map_type == ProjectProperties.BaseMapType.SINGLE_LAYER:
            # the `native:tilesxyzmbtiles` alg does not have any LAYERS param
            # so just add basemap layer to the cloned project
            basemap_layer = current_project.mapLayer(
                self.project_configuration.base_map_layer
            )
            # here we use a cloned version of the raster layer, otherwise QGIS might crash
            clone_layer = basemap_layer.clone()
            cloned_project.addMapLayer(clone_layer)

        elif base_map_type == ProjectProperties.BaseMapType.MAP_THEME:
            # clone and recreate the current QGIS project, and recreate original themes
            current_themes = QgsMapThemeCollection(current_project)
            themes_data = {}

            for theme_name in current_themes.mapThemes():
                layers, visibility = current_themes.mapThemeLayers(theme_name)
                themes_data[theme_name] = (layers, visibility)

            # create a temp file to store current QGIS project
            with tempfile.NamedTemporaryFile(suffix=".qgz", delete=False) as temp_file:
                temp_path = temp_file.name

            current_project.write(temp_path)

            cloned_project.read(temp_path)

            cloned_themes_collection = QgsMapThemeCollection(cloned_project)
            for theme_name, (layers, visibility) in themes_data.items():
                cloned_themes_collection.storeMapTheme(theme_name, layers, visibility)

            layer_tree_root = cloned_project.layerTreeRoot()
            layer_tree_model = QgsLayerTreeModel(layer_tree_root)
            cloned_project.mapThemeCollection().applyTheme(
                self.project_configuration.base_map_theme,
                layer_tree_root,
                layer_tree_model,
            )

        context = QgsProcessingContext()
        context.setProject(cloned_project)

        # connect subtask feedback progress signal
        self._feedback.progressChanged.connect(self._on_tiles_gen_alg_progress_changed)

        # we use a try clause to make sure the feedback's `progressChanged` signal
        # is disconnected in the finally clause.
        try:
            # if the basemap file already exists on target destination,
            # the `native:tilesxyzmbtiles` alg will throw an error.
            basemap_export_path.unlink(missing_ok=True)

            results, ok = alg.run(params, context, self._feedback)

            if not ok:
                self.warning.emit(
                    self.tr("Failed to create mbtiles basemap"),
                    self._feedback.textLog(),
                )
                return False

            new_layer = QgsRasterLayer(results["OUTPUT_FILE"], self.tr("Basemap"))

            self.project_configuration.project.addMapLayer(new_layer, False)

            layer_tree = QgsProject.instance().layerTreeRoot()
            layer_tree.insertLayer(len(layer_tree.children()), new_layer)

            return True

        finally:
            self._feedback.progressChanged.disconnect(
                self._on_tiles_gen_alg_progress_changed
            )

    def _on_tiles_gen_alg_progress_changed(self, revision: float) -> None:
        """
        Called when the native `native:tilesxyzmbtiles` algorithm's execution emits progress.
        This method will notify the accurate signal about this progress, e.g. QFieldSync progress bar UI.

        Args:
            revision (float): progress value of the tiles generation algorithm (between 0 and 100)

        """
        self.task_progress_updated.emit(int(revision), 100)

    def _on_offline_editing_next_layer(self, layer_index, layer_count):
        msg = self.trUtf8("Packaging layer {layer_name}…").format(
            layer_name=self.__offline_layer_names[layer_index - 1]
        )
        self.total_progress_updated.emit(layer_index, layer_count, msg)

    def _on_offline_editing_max_changed(self, _, mode_count):
        self.__max_task_progress = mode_count

    def _on_original_project_read_wrapper(self, elements):
        def on_original_project_read(doc):
            if not elements.get("map_canvas"):
                elements["map_canvas"] = elements.get(
                    "map_canvas", get_themapcanvas(doc)
                )

        return on_original_project_read

    def _on_original_project_write_wrapper(self, elements):
        def on_original_project_write(doc):
            if not get_themapcanvas(doc) and elements.get("map_canvas"):
                doc.elementsByTagName("qgis").at(0).appendChild(
                    elements.get("map_canvas")
                )

        return on_original_project_write

    def _on_offline_editing_task_progress(self, progress):
        self.task_progress_updated.emit(progress, self.__max_task_progress)

    def cancel(self) -> None:
        """
        Cancels the offline packaging of a QField project.
        Typically used when the QField export dialog is closed.
        """
        self._is_canceled = True
        self._feedback.cancel()

    def _check_canceled(self) -> None:
        """Checks if packaging has been and should be canceled."""
        QCoreApplication.processEvents()
        if self._is_canceled:
            raise PackagingCanceledError()
