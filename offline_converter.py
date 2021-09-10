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
import traceback
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from qgis.core import (
    QgsApplication,
    QgsBilinearRasterResampler,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCubicRasterResampler,
    QgsEditorWidgetSetup,
    QgsField,
    QgsFields,
    QgsMapLayer,
    QgsOfflineEditing,
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
from .project import ProjectConfiguration, ProjectProperties
from .utils.file_utils import copy_images, isascii
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
        pk_names: Optional[List[str]]


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
        export_folder: str,
        area_of_interest_wkt: str,
        area_of_interest_epsg: str,
        offline_editing: QgsOfflineEditing,
        export_type: ExportType = ExportType.Cable,
    ):

        super(OfflineConverter, self).__init__(parent=None)
        self.__max_task_progress = 0
        self.__offline_layers = []
        self.__convertor_progress = None  # for processing feedback
        self.__layers = list()
        self.__layer_data_by_id: Dict[str, LayerData] = {}
        self.__layer_data_by_name: Dict[str, LayerData] = {}
        self.__offline_layer_names: List[str] = []

        # elipsis workaround
        self.trUtf8 = self.tr

        self.export_folder = Path(export_folder)
        self.export_type = export_type
        self.area_of_interest = QgsPolygon()
        self.area_of_interest.fromWkt(area_of_interest_wkt)
        self.area_of_interest_crs = QgsCoordinateReferenceSystem(area_of_interest_epsg)

        assert self.area_of_interest.isValid()
        assert self.area_of_interest_crs.isValid()

        self.offline_editing = offline_editing
        self.project_configuration = ProjectConfiguration(project)

    def convert(self) -> None:
        """
        Convert the project to a portable project.
        """
        project = QgsProject.instance()
        self.original_filename = Path(project.fileName())
        self.backup_filename = make_temp_qgis_file(project)

        self.offline_editing.layerProgressUpdated.connect(
            self._on_offline_editing_next_layer
        )
        self.offline_editing.progressModeSet.connect(
            self._on_offline_editing_max_changed
        )
        self.offline_editing.progressUpdated.connect(self.offline_editing_task_progress)

        try:
            self._convert(project)
        except Exception as err:
            (_type, _value, tb) = sys.exc_info()
            print("error", str(err))
            print("error_origin", "orchestrator")
            print("error_stack", traceback.format_tb(tb))
        finally:
            QCoreApplication.processEvents()
            QgsProject.instance().clear()
            QCoreApplication.processEvents()

            open_project(str(self.original_filename), self.backup_filename)

            self.offline_editing.layerProgressUpdated.disconnect(
                self._on_offline_editing_next_layer
            )
            self.offline_editing.progressModeSet.disconnect(
                self._on_offline_editing_max_changed
            )
            self.offline_editing.progressUpdated.disconnect(
                self.offline_editing_task_progress
            )
            self.total_progress_updated.emit(100, 100, self.tr("Finished"))

    def _convert(self, project: QgsProject) -> None:
        project.baseName()
        xml_elements_to_preserve = {}
        on_original_project_read = self._on_original_project_read_wrapper(
            xml_elements_to_preserve
        )
        project.readProject.connect(on_original_project_read)
        project.read(project.fileName())
        project.readProject.disconnect(on_original_project_read)

        self.export_folder.mkdir(parents=True, exist_ok=True)
        self.total_progress_updated.emit(0, 100, self.trUtf8("Converting project…"))
        self.__layers = list(project.mapLayers().values())

        # We store the pks of the original vector layers
        for layer in self.__layers:
            pk_names = None
            if layer.type() == QgsMapLayer.VectorLayer:
                pk_names = []
                for idx in layer.primaryKeyAttributes():
                    pk_name = layer.fields()[idx].name()
                    # and we check that the primary key fields names don't have a comma in the name
                    if "," in pk_name:
                        raise ValueError("Comma in field names not allowed")
                    pk_names.append(pk_name)
                    layer.setCustomProperty(
                        "QFieldSync/sourceDataPrimaryKeys", ",".join(pk_names)
                    )

            layer_data: LayerData = {
                "id": layer.id(),
                "name": layer.name(),
                "type": layer.type(),
                "source": layer.source(),
                "fields": layer.fields() if hasattr(layer, "fields") else None,
                "pk_names": pk_names,
            }

            self.__layer_data_by_id[layer.id()] = layer_data
            self.__layer_data_by_name[layer.name()] = layer_data

        if self.project_configuration.create_base_map:
            self._export_basemap()

        # Loop through all layers and copy/remove/offline them
        copied_files = list()
        for layer_idx, layer in enumerate(self.__layers):
            self.total_progress_updated.emit(
                layer_idx - len(self.__offline_layers),
                len(self.__layers),
                self.trUtf8("Copying layers…"),
            )

            layer_source = LayerSource(layer)
            layer_action = (
                layer_source.action
                if self.export_type == ExportType.Cable
                else layer_source.cloud_action
            )

            if not layer.isValid():
                project.removeMapLayer(layer)
                continue

            if not layer_source.is_supported:
                project.removeMapLayer(layer)
                continue

            if layer_source.is_file and isascii(layer_source.filename):
                self.warning.emit(
                    self.tr("QFieldSync"),
                    self.tr(
                        'Layer "{}" stored at "{}" is not using ASCII encoded filename. Working with the layer might cause problems, please rename it using only ASCII charasters.'
                    ).format(
                        layer.name(),
                        layer_source.filename,
                    ),
                )

            if layer_source.is_localized_path:
                continue

            if (
                layer.type() == QgsMapLayer.VectorLayer
                and layer.dataProvider().encoding() != "UTF-8"
            ):
                self.warning.emit(
                    self.tr("QFieldSync"),
                    self.tr(
                        'Layer "{}" is not using UTF-8 encoding, but "{}". Working with the layer might cause problems, please convert it to UTF-8 layer.'
                    ).format(
                        layer.name(),
                        layer.dataProvider().encoding(),
                    ),
                )

            if layer_action == SyncAction.OFFLINE:
                if (
                    self.project_configuration.offline_copy_only_aoi
                    and not self.project_configuration.offline_copy_only_selected_features
                ):
                    extent = QgsCoordinateTransform(
                        QgsCoordinateReferenceSystem(self.area_of_interest_crs),
                        layer.crs(),
                        QgsProject.instance(),
                    ).transformBoundingBox(self.area_of_interest.boundingBox())
                    layer.selectByRect(extent)
                elif (
                    self.project_configuration.offline_copy_only_aoi
                    and self.project_configuration.offline_copy_only_selected_features
                ):
                    # This option is only possible via API
                    QgsApplication.instance().messageLog().logMessage(
                        self.tr(
                            'Both "Area of Interest" and "only selected features" options were enabled, tha latter takes precedence.'
                        ),
                        "QFieldSync",
                    )

                if not layer.selectedFeatureCount():
                    layer.selectByIds([FID_NULL])

                self.__offline_layers.append(layer)
                self.__offline_layer_names.append(layer.name())
            elif (
                layer_action == SyncAction.COPY or layer_action == SyncAction.NO_ACTION
            ):
                copied_files = layer_source.copy(self.export_folder, copied_files)
            elif layer_action == SyncAction.KEEP_EXISTENT:
                layer_source.copy(self.export_folder, copied_files, True)
            elif layer_action == SyncAction.REMOVE:
                project.removeMapLayer(layer)

        export_project_filename = self.export_folder.joinpath(
            f"{self.original_filename.stem}_qfield.qgs"
        )

        # save the original project path
        self.project_configuration.original_project_path = str(self.original_filename)

        # save the offline project twice so that the offline plugin can "know" that it's a relative path
        QgsProject.instance().write(str(export_project_filename))

        # export the DCIM folder
        copy_images(
            str(self.original_filename.parent.joinpath("DCIM")),
            str(export_project_filename.parent.joinpath("DCIM")),
        )
        try:
            # Run the offline plugin for gpkg
            gpkg_filename = "data.gpkg"
            if self.__offline_layers:
                offline_layer_ids = [o_l.id() for o_l in self.__offline_layers]
                only_selected = (
                    self.project_configuration.offline_copy_only_aoi
                    or self.project_configuration.offline_copy_only_selected_features
                )

                if not self.offline_editing.convertToOfflineProject(
                    str(self.export_folder),
                    gpkg_filename,
                    offline_layer_ids,
                    only_selected,
                    self.offline_editing.GPKG,
                    None,
                ):
                    raise Exception(
                        self.tr("Error trying to convert layers to offline layers")
                    )
        except AttributeError:
            # Run the offline plugin for spatialite
            spatialite_filename = "data.sqlite"
            if self.__offline_layers:
                offline_layer_ids = [o_l.id() for o_l in self.__offline_layers]
                only_selected = (
                    self.project_configuration.offline_copy_only_aoi
                    or self.project_configuration.offline_copy_only_selected_features
                )

                if not self.offline_editing.convertToOfflineProject(
                    str(self.export_folder),
                    spatialite_filename,
                    offline_layer_ids,
                    only_selected,
                    self.offline_editing.SpatiaLite,
                    None,
                ):
                    raise Exception(
                        self.tr("Error trying to convert layers to offline layers")
                    )

        # Disable project options that could create problems on a portable
        # project with offline layers
        self.post_process_layers()

        # Now we have a project state which can be saved as offline project
        on_original_project_write = self._on_original_project_write_wrapper(
            xml_elements_to_preserve
        )
        project.writeProject.connect(on_original_project_write)
        QgsProject.instance().write(str(export_project_filename))
        project.writeProject.disconnect(on_original_project_write)

    def post_process_layers(self):
        if not self.__offline_layers:
            return

        project = QgsProject.instance()
        project.setEvaluateDefaultValues(False)
        project.setAutoTransaction(False)

        # check if value relations point to offline layers and adjust if necessary
        for e_layer in project.mapLayers().values():
            if e_layer.type() == QgsMapLayer.VectorLayer:
                # Before QGIS 3.14 the custom properties of a layer are not
                # kept into the new layer during the conversion to offline project
                # So we try to identify the new created layer by its name and
                # we set the custom properties again.
                if not e_layer.customProperty("QFieldSync/sourceDataPrimaryKeys"):
                    o_layer_name = e_layer.name().rsplit(" ", 1)[0]
                    o_layer_data = self.__layer_data_by_name.get(o_layer_name, None)

                    if not o_layer_data:
                        self.warning.emit(
                            self.tr("QFieldSync"),
                            self.tr(
                                'Failed to find layer with name "{}". QFieldSync will not package that layer.'
                            ).format(o_layer_name),
                        )
                        continue

                    e_layer.setCustomProperty("remoteLayerId", o_layer_data["id"])
                    e_layer.setCustomProperty(
                        "QFieldSync/sourceDataPrimaryKeys",
                        o_layer_data["pk_names"],
                    )

                if (
                    not e_layer.customProperty("remoteLayerId")
                    or e_layer.customProperty("remoteLayerId")
                    not in self.__layer_data_by_id
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
        e_layer_source = LayerSource(e_layer)
        o_layer_data = self.__layer_data_by_id[e_layer.customProperty("remoteLayerId")]
        o_layer_fields: QgsFields = o_layer_data["fields"]
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

        extent = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem(self.area_of_interest_crs),
            project.mapLayer(self.project_configuration.base_map_layer).crs(),
            project,
        ).transformBoundingBox(basemap_extent)

        extent_string = "{},{},{},{}".format(
            extent.xMinimum(),
            extent.xMaximum(),
            extent.yMinimum(),
            extent.yMaximum(),
        )

        alg = (
            QgsApplication.instance()
            .processingRegistry()
            .createAlgorithmById("qgis:rasterize")
        )

        params = {
            "EXTENT": extent_string,
            "MAP_UNITS_PER_PIXEL": self.project_configuration.base_map_mupp,
            "TILE_SIZE": self.project_configuration.base_map_tile_size,
            "MAKE_BACKGROUND_TRANSPARENT": False,
            "OUTPUT": os.path.join(self.export_folder, "basemap.gpkg"),
        }

        base_map_type = self.project_configuration.base_map_type
        if base_map_type == ProjectProperties.BaseMapType.SINGLE_LAYER:
            params["LAYER"] = self.project_configuration.base_map_layer
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

    def offline_editing_task_progress(self, progress):
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
