# -*- coding: utf-8 -*-

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

import os
import sys
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Union

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsBilinearRasterResampler,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCubicRasterResampler,
    QgsEditorWidgetSetup,
    QgsField,
    QgsFields,
    QgsMapLayer,
    QgsPolygon,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsValueRelationFieldFormatter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QObject, pyqtSignal

from .layer import LayerSource, SyncAction
from .offliners import BaseOffliner
from .project import ProjectConfiguration, ProjectProperties
from .utils.file_utils import copy_attachments, copy_multifile
from .utils.logger import logger
from .utils.qgis import make_temp_qgis_file, open_project
from .utils.xml import get_themapcanvas

FID_NULL = -4294967296

if sys.version_info >= (3, 8):
    from typing import TypedDict

    class LayerData(TypedDict):
        id: str
        name: str
        source: str
        type: int
        fields: Optional[QgsFields]

else:
    LayerData = Dict


class ExportType(Enum):
    Cable = "cable"
    Cloud = "cloud"


class OfflineConverter(QObject):
    progressStopped = pyqtSignal()
    warning = pyqtSignal(str, str)
    task_progress_updated = pyqtSignal(int, int)
    total_progress_updated = pyqtSignal(int, int, str)

    def __init__(
        self,
        project: QgsProject,
        export_filename: str,
        area_of_interest_wkt: str,
        area_of_interest_crs: Union[str, QgsCoordinateReferenceSystem],
        attachment_dirs: List[str],
        offliner: BaseOffliner,
        export_type: ExportType = ExportType.Cable,
        create_basemap: bool = True,
        dirs_to_copy: Optional[Dict[str, bool]] = None,
        export_title: Optional[str] = None,
    ):
        super(OfflineConverter, self).__init__(parent=None)
        self.__max_task_progress = 0
        self.__convertor_progress = None  # for processing feedback
        self.__layer_data_by_id: Dict[str, LayerData] = {}
        self.__offline_layer_names: List[str] = []

        # elipsis workaround
        self.trUtf8 = self.tr

        self.export_folder = Path(export_filename).parent
        self._export_filename = Path(export_filename).stem
        self._export_title = export_title
        self.export_type = export_type
        self.create_basemap = create_basemap
        self.area_of_interest = QgsPolygon()
        self.area_of_interest.fromWkt(area_of_interest_wkt)
        self.area_of_interest_crs = QgsCoordinateReferenceSystem(area_of_interest_crs)
        self.attachment_dirs = attachment_dirs
        self.dirs_to_copy = dirs_to_copy

        self.offliner = offliner

        self.offliner.layerProgressUpdated.connect(self._on_offline_editing_next_layer)
        self.offliner.progressModeSet.connect(self._on_offline_editing_max_changed)
        self.offliner.progressUpdated.connect(self._on_offline_editing_task_progress)

        self.project_configuration = ProjectConfiguration(project)

        if (
            self.project_configuration.offline_copy_only_aoi
            or self.project_configuration.create_base_map
        ):
            assert self.area_of_interest.isValid()[0]
            assert (
                self.area_of_interest_crs.isValid()
            ), f"Invalid CRS specified for area of interest {area_of_interest_crs}"

    # flake8: noqa: max-complexity: 33
    def convert(self, reload_original_project: bool = True) -> None:
        """
        Convert the project to a portable project.
        """
        project = QgsProject.instance()
        self.original_filename = Path(project.fileName())
        self.backup_filename = make_temp_qgis_file(project, self._export_title)

        try:
            self._convert(project)
        finally:
            if reload_original_project:
                QCoreApplication.processEvents()
                QgsProject.instance().clear()
                QCoreApplication.processEvents()

                open_project(str(self.original_filename), self.backup_filename)

            self.total_progress_updated.emit(100, 100, self.tr("Finished"))

    def _convert(self, project: QgsProject) -> None:
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
        if Qgis.versionInt() >= 32600:
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

        # NOTE force delete the `QgsProject`, otherwise the `QgsApplication` might be deleted by the time the project is garbage collected
        del tmp_project

        self.export_folder.mkdir(parents=True, exist_ok=True)
        self.total_progress_updated.emit(0, 100, self.trUtf8("Converting project…"))

        project_layers: List[QgsMapLayer] = list(project.mapLayers().values())
        offline_layers: List[QgsMapLayer] = []
        copied_files = list()

        if self.create_basemap and self.project_configuration.create_base_map:
            self._export_basemap()

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
                    else:
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

            if layer_action == SyncAction.OFFLINE:
                offline_layers.append(layer)
                self.__offline_layer_names.append(layer.name())
            elif (
                layer_action == SyncAction.COPY or layer_action == SyncAction.NO_ACTION
            ):
                copied_files = layer_source.copy(self.export_folder, copied_files)
            elif layer_action == SyncAction.KEEP_EXISTENT:
                layer_source.copy(self.export_folder, copied_files, True)
            elif layer_action == SyncAction.REMOVE:
                project.removeMapLayer(layer)

        export_project_filename = Path(self.export_folder).joinpath(
            f"{self._export_filename}.qgs"
        )

        # save the original project path
        self.project_configuration.original_project_path = str(self.original_filename)

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

            copy_attachments(
                self.original_filename.parent,
                export_project_filename.parent,
                Path(source_dir),
            )

        # copy project plugin if present
        plugin_file = Path("{}.qml".format(str(self.original_filename)[:-4]))
        if plugin_file.exists():
            copy_multifile(
                plugin_file, export_project_filename.parent.joinpath(plugin_file.name)
            )

        gpkg_filename = str(self.export_folder.joinpath("data.gpkg"))
        if offline_layers:
            bbox = None
            if self.project_configuration.offline_copy_only_aoi:
                bbox = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem(self.area_of_interest_crs),
                    QgsProject.instance().crs(),
                    QgsProject.instance(),
                ).transformBoundingBox(self.area_of_interest.boundingBox())

            is_success = self.offliner.convert_to_offline(
                gpkg_filename, offline_layers, bbox, self._export_title
            )

            if not is_success:
                raise Exception(
                    self.tr(
                        "QGIS Offline editing error: failed to convert layers to offline layers"
                    )
                )

            # Disable project options that could create problems on a portable
            # project with offline layers
            self.post_process_offline_layers()

        # Now we have a project state which can be saved as offline project
        on_original_project_write = self._on_original_project_write_wrapper(
            xml_elements_to_preserve
        )
        project.writeProject.connect(on_original_project_write)
        QgsProject.instance().write(str(export_project_filename))
        project.writeProject.disconnect(on_original_project_write)

    def post_process_offline_layers(self):
        project = QgsProject.instance()
        project.setEvaluateDefaultValues(False)
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
        o_layer_fields: QgsFields = o_layer_data["fields"]  # type: ignore
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
        for e_layer in project.mapLayers().values():
            o_layer_data = self.__layer_data_by_id[o_referenced_layer_id]

            if e_layer.customProperty("remoteSource") == o_layer_data["source"]:
                #  First try strict matching: the offline layer should have a "remoteSource" property
                e_referenced_layer_id = e_layer.id()
                break
            elif e_layer.name() == o_layer_data["name"]:
                #  If that did not work, go with loose matching
                e_referenced_layer_id = e_layer.id()
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
            import qgis.utils

            # TODO investigate why starPlugin fails in docker
            # print(1111111111010301, qgis.utils.loadPlugin("processing"))
            # print(1111111111010302, qgis.utils.startPlugin("processing"))

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
            pass

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

        extent_string = "{},{},{},{}".format(
            extent.xMinimum(),
            extent.xMaximum(),
            extent.yMinimum(),
            extent.yMaximum(),
        )

        alg = (
            QgsApplication.instance()
            .processingRegistry()
            .createAlgorithmById("native:rasterize")
        )

        params = {
            "EXTENT": extent_string,
            "EXTENT_BUFFER": 0,
            "TILE_SIZE": self.project_configuration.base_map_tile_size,
            "MAP_UNITS_PER_PIXEL": self.project_configuration.base_map_mupp,
            "MAKE_BACKGROUND_TRANSPARENT": False,
            "OUTPUT": os.path.join(self.export_folder, "basemap.gpkg"),
        }

        if base_map_type == ProjectProperties.BaseMapType.SINGLE_LAYER:
            params["LAYERS"] = [self.project_configuration.base_map_layer]
        elif base_map_type == ProjectProperties.BaseMapType.MAP_THEME:
            params["MAP_THEME"] = self.project_configuration.base_map_theme

        feedback = QgsProcessingFeedback()
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())

        results, ok = alg.run(params, context, feedback)

        if not ok:
            self.warning.emit(self.tr("Failed to create basemap"), feedback.textLog())
            return False

        new_layer = QgsRasterLayer(results["OUTPUT"], self.tr("Basemap"))

        resample_filter = new_layer.resampleFilter()
        resample_filter.setZoomedInResampler(QgsCubicRasterResampler())
        resample_filter.setZoomedOutResampler(QgsBilinearRasterResampler())
        self.project_configuration.project.addMapLayer(new_layer, False)
        layer_tree = QgsProject.instance().layerTreeRoot()
        layer_tree.insertLayer(len(layer_tree.children()), new_layer)

        return True

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

    def convertorProcessingProgress(self):
        """
        Will create a new progress object for processing to get feedback from the basemap
        algorithm.
        """

        class ConverterProgress(QObject):
            progress_updated = pyqtSignal(int, int)

            def __init__(self):
                QObject.__init__(self)

            def error(self, msg):
                pass

            def setText(self, msg):
                pass

            def setPercentage(self, i):
                self.progress_updated.emit(i, 100)
                QCoreApplication.processEvents()

            def setInfo(self, msg):
                pass

            def setCommand(self, msg):
                pass

            def setDebugInfo(self, msg):
                pass

            def setConsoleInfo(self, msg):
                pass

            def close(self):
                pass

        if not self.__convertor_progress:
            self.__convertor_progress = ConverterProgress()
            self.__convertor_progress.progress_updated.connect(
                self.task_progress_updated
            )

        return self.__convertor_progress
