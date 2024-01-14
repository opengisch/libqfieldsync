import hashlib
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import List, NamedTuple, Optional

from osgeo import ogr, osr
from PyQt5.QtCore import QFileInfo, QVariant
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsField,
    QgsFieldConstraints,
    QgsJsonUtils,
    QgsMapLayer,
    QgsOfflineEditing,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    edit,
)
from qgis.PyQt.QtCore import QObject, pyqtSignal

from .utils.logger import logger

FID_NULL = -4294967296

CUSTOM_PROPERTY_IS_OFFLINE_EDITABLE = "isOfflineEditable"
CUSTOM_PROPERTY_REMOTE_SOURCE = "remoteSource"
CUSTOM_PROPERTY_REMOTE_PROVIDER = "remoteProvider"
CUSTOM_SHOW_FEATURE_COUNT = "showFeatureCount"
CUSTOM_PROPERTY_ORIGINAL_LAYERID = "remoteLayerId"
CUSTOM_PROPERTY_LAYERNAME_SUFFIX = "layerNameSuffix"
PROJECT_ENTRY_SCOPE_OFFLINE = "OfflineEditingPlugin"
PROJECT_ENTRY_KEY_OFFLINE_DB_PATH = "/OfflineDbPath"


class OfflinerType(str, Enum):
    QGISCORE = "qgiscore"
    PYTHONMINI = "pythonmini"


class BaseOffliner(QObject):
    warning = pyqtSignal(str, str)
    layerProgressUpdated = pyqtSignal(int, int)
    progressModeSet = pyqtSignal(QgsOfflineEditing.ProgressMode, int)
    progressUpdated = pyqtSignal(int)

    def convert_to_offline(
        self,
        offline_db_filename: str,
        layers: List[QgsMapLayer],
        bbox: Optional[QgsRectangle],
    ) -> bool:
        raise NotImplementedError(
            "Expected `BaseOffliner` to be extended by a class that implements `convert_to_offline`."
        )


class QgisCoreOffliner(BaseOffliner):
    def __init__(self, *args, **kwargs) -> None:
        # We don't pass `QgsOfflineEditing` as a second argument to `dict.pop()`,
        # because it will create a useless instance, therefore we pass it as a second operand to `or`.
        offline_editing = kwargs.pop("offline_editing", None) or QgsOfflineEditing()
        super().__init__(*args, **kwargs)
        self.offliner = offline_editing

        # Check https://api.qgis.org/api/3.14/classQgsOfflineEditing.html#a59d2ebed32704f655868951eba6ef52e for more documentation of these signals
        # NOTE directly connecting the slot like `self.offliner.progressModeSet.connect(self.progressModeSet)` raises typing error
        self.offliner.layerProgressUpdated.connect(
            lambda progress, layer_idx: self.layerProgressUpdated.emit(
                progress, layer_idx
            )
        )
        self.offliner.progressModeSet.connect(
            lambda mode, maximum: self.progressModeSet.emit(mode, maximum)
        )
        self.offliner.progressUpdated.connect(
            lambda progress: self.progressUpdated.emit(progress)
        )

    def convert_to_offline(
        self,
        offline_db_filename: str,
        layers: List[QgsMapLayer],
        bbox: Optional[QgsRectangle],
    ) -> bool:
        offline_db_path = Path(offline_db_filename).parent
        layer_ids = [layer.id() for layer in layers]

        only_selected = False
        # If `bbox` is valid and not empty
        if bbox and bbox.isFinite():
            only_selected = True
            for layer in layers:
                if Qgis.QGIS_VERSION_INT >= 33000:
                    no_geometry_types = [
                        Qgis.GeometryType.Null,
                        Qgis.GeometryType.Unknown,
                    ]
                else:
                    from qgis.core import QgsWkbTypes

                    no_geometry_types = [
                        QgsWkbTypes.GeometryType.NullGeometry,
                        QgsWkbTypes.GeometryType.UnknownGeometry,
                    ]

                if layer.geometryType() in no_geometry_types:
                    # ensure that geometry-less layers do not have selected features that would interfere with the process
                    layer.removeSelection()
                else:
                    layer.selectByRect(bbox)

                if layer.selectedFeatureCount() == 0:
                    layer.selectByIds([FID_NULL])

        is_success = self.offliner.convertToOfflineProject(
            str(offline_db_path),
            str(offline_db_filename),
            layer_ids,
            only_selected,
            # containerType - GPKG or SpatiaLite
            containerType=QgsOfflineEditing.GPKG,
            # layerNameSuffix - by default " (offlined)" is added as suffix
            layerNameSuffix=None,
        )

        return is_success


class PythonMiniOffliner(BaseOffliner):
    def convert_to_offline(
        self,
        offline_db_filename: str,
        layers: List[QgsMapLayer],
        bbox: Optional[QgsRectangle],
    ) -> bool:
        self._convert_to_offline_project(str(offline_db_filename), layers, bbox)
        return True

    def ogr_field_type(self, field: QgsField) -> ogr.FieldDefn:
        """
        Converts a QGIS field type to a matching OGR field type
        """
        ogr_sub_type = ogr.OFSTNone

        type = field.type()

        if type == QVariant.Int:
            ogr_type = ogr.OFTInteger
        elif type == QVariant.LongLong:
            ogr_type = ogr.OFTInteger64
        elif type == QVariant.Double:
            ogr_type = ogr.OFTReal
        elif type == QVariant.Time:
            ogr_type = ogr.OFTTime
        elif type == QVariant.Date:
            ogr_type = ogr.OFTDate
        elif type == QVariant.DateTime:
            ogr_type = ogr.OFTDateTime
        elif type == QVariant.Bool:
            ogr_type = ogr.OFTInteger
            ogr_sub_type = ogr.OFSTBoolean
        elif type == QVariant.StringList or type == QVariant.List:
            ogr_type = ogr.OFTString
            ogr_sub_type = ogr.OFSTJSON
        else:
            ogr_type = ogr.OFTString

        ogr_width = field.length()

        ogr_field = ogr.FieldDefn(field.name(), ogr_type)
        if ogr_sub_type != ogr.OFSTNone:
            ogr_field.SetSubType(ogr_sub_type)
        ogr_field.SetWidth(ogr_width)

        return ogr_field

    def qgis_crs_to_ogr_srs(
        self, crs: QgsCoordinateReferenceSystem
    ) -> osr.SpatialReference:
        """Converts a QGIS CRS to an OGR CRS."""
        auth_id = crs.authid()
        srs_wkt = crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED_GDAL)
        ogr_srs = osr.SpatialReference()

        if auth_id:
            ogr_srs.SetFromUserInput(auth_id)

        if ogr_srs.Validate() != 0:
            ogr_srs.SetFromUserInput(srs_wkt)

        return ogr_srs

    def ogr_escape(self, data: str):
        # There is no such thing as escaping for gdal options
        # CSLFetchNameValue compares the name of an option followed by a `=` or `:` and treats everything after as value.
        return data

    def create_layer(
        self, layer: QgsVectorLayer, data_source: ogr.DataSource, offline_gpkg_path: str
    ) -> None:
        """
        Will create a new layer for ``layer`` in the GeoPackage specified as ``data_source`` which is stored at ``offline_gpkg_path``.
        """

        identifier = hashlib.sha256(
            layer.dataProvider().dataSourceUri().encode()
        ).hexdigest()

        layer_options = [
            "OVERWRITE=YES",
            f"IDENTIFIER={self.ogr_escape(identifier)}",
            f"DESCRIPTION={self.ogr_escape(layer.dataComment())}",
        ]

        fid = "fid"
        counter = 1
        while layer.dataProvider().fields().lookupField(fid) >= 0:
            fid = f"fid_{counter}"
            counter += 1
            if counter == 10000:
                raise RuntimeError(
                    f"Cannot determine usable FID field name for GPKG {layer.name()}"
                )

        layer_options.append(f"FID={fid}")

        if layer.isSpatial():
            layer_options.append("GEOMETRY_COLUMN=geom")
            layer_options.append("SPATIAL_INDEX=YES")

        ogr_srs = self.qgis_crs_to_ogr_srs(layer.crs())

        ogr_layer = data_source.CreateLayer(
            identifier, geom_type=layer.wkbType(), options=layer_options, srs=ogr_srs
        )

        fields = layer.dataProvider().fields()
        for field in fields:
            result = ogr_layer.CreateField(self.ogr_field_type(field))
            if result:
                raise RuntimeError(
                    f"Creating field for {layer.name()}.{field.name()}({field.typeName()}) failed"
                )

        ogr_layer.SyncToDisk()

    def convert_to_offline_layer(
        self,
        layer: QgsVectorLayer,
        data_source: ogr.DataSource,
        offline_gpkg_path: str,
        feature_request: QgsFeatureRequest = QgsFeatureRequest(),
    ) -> str:
        """
        Will fill a copy of ``layer`` in the GeoPackage specified as ``data_source`` which is stored at ``offline_gpkg_path``.
        It will replace the dataProvider of the original layer.
        """

        identifier = hashlib.sha256(
            layer.dataProvider().dataSourceUri().encode()
        ).hexdigest()

        qgis_uri = f"{offline_gpkg_path}|layername={identifier}"

        qgis_layer_options = QgsVectorLayer.LayerOptions(
            QgsProject.instance().transformContext()
        )

        new_layer = QgsVectorLayer(qgis_uri, identifier, "ogr", qgis_layer_options)

        if not new_layer.isValid():
            raise RuntimeError(
                f"We were not able to create the layer {layer.name()} ..."
            )

        with edit(new_layer):
            feature_request = QgsFeatureRequest()

            new_fields = new_layer.fields()

            for feature in layer.dataProvider().getFeatures(feature_request):
                # Prepend an empty attribute for the new FID
                attrs = [None] + feature.attributes()

                # Fixup list and json attributes
                for i in range(new_layer.fields().count()):
                    type = new_layer.fields().at(i).type()
                    if type == QVariant.StringList or type == QVariant.List:
                        attrs[i] = QgsJsonUtils.encodeValue(attrs[i])

                feature.setFields(new_fields)
                feature.setAttributes(attrs)
                new_layer.addFeature(feature)

        return new_layer.source()

    def update_data_provider(self, layer: QgsVectorLayer, source: str) -> None:
        # Mark as offline layer
        layer.setCustomProperty(CUSTOM_PROPERTY_IS_OFFLINE_EDITABLE, True)

        # store original layer source and information
        layer.setCustomProperty(CUSTOM_PROPERTY_REMOTE_SOURCE, layer.source())
        layer.setCustomProperty(CUSTOM_PROPERTY_REMOTE_PROVIDER, layer.providerType())
        layer.setCustomProperty(CUSTOM_PROPERTY_ORIGINAL_LAYERID, layer.id())
        layer.setCustomProperty(CUSTOM_PROPERTY_LAYERNAME_SUFFIX, "")

        # Remove constraints from fields that have a default value provided by the "online" provider.
        # Example: a user should not be forced to enter an autogenerated primary key in offline mode, compare https://github.com/qgis/QGIS/issues/28122
        not_null_field_names = []
        source_fields = layer.fields()
        for field in source_fields:
            if layer.dataProvider().defaultValueClause(
                layer.fields().fieldOriginIndex(layer.fields().indexOf(field.name()))
            ):
                not_null_field_names.append(field.name())

        layer.setDataSource(source, layer.name(), "ogr")

        for field in source_fields:
            index = layer.fields().indexOf(field.name())
            if index > -1:
                # restore unique value constraints coming from original data provider
                if (
                    field.constraints().constraints()
                    & QgsFieldConstraints.ConstraintUnique
                ):
                    layer.setFieldConstraint(
                        index, QgsFieldConstraints.ConstraintUnique
                    )

                # remove any undesired not null constraints coming from original data provider
                if field.name() in not_null_field_names:
                    layer.removeFieldConstraint(
                        index, QgsFieldConstraints.ConstraintNotNull
                    )

    def _convert_to_offline_project(
        self,
        offline_gpkg_path: str,
        offline_layers: Optional[List[QgsMapLayer]],
        bbox: Optional[QgsRectangle],
    ) -> None:
        """Converts the currently loaded QgsProject to an offline project.
        Offline layers are written to ``offline_gpkg_path``. Only valid vector layers are written.
        If ``layer_ids`` is specified, only layers present in this list are written.
        If ``bbox`` is specified, only features within this ``bbox`` are written.

        NOTE `QgsOfflineEditing` sets `PRAGMA FOREIGN_KEY`, but this implementation does not,
        as dealing with FK on the field is pain for the user, manager and developers.
        We leave any FK mismatches during sync. See GH comment about this: https://github.com/opengisch/libqfieldsync/pull/54/files#r1450173731
        NOTE `QgsOfflineEditing` calls `Initialize Spatial Metadata`, but this
        implementation does not, as it is considered a spatialite leftover.
        """
        project = QgsProject.instance()

        driver = ogr.GetDriverByName("GPKG")
        data_source = driver.CreateDataSource(offline_gpkg_path)

        class LayerInfo(NamedTuple):
            layer: QgsVectorLayer
            subset_string: str

        # A dict that maps data sources (tables) to a list of layers connecting them
        datasource_mapping = defaultdict(list)
        for layer in project.mapLayers().values():
            if layer.type() != QgsMapLayer.VectorLayer:
                logger.info(f"Skipping layer {layer.name()} :: not a vector layer")
                continue

            if not layer.isValid():
                reason = ""
                if layer.dataProvider():
                    reason = layer.dataProvider().error()
                logger.info(f"Skipping layer {layer.name()} :: invalid ({reason})")
                continue

            if offline_layers is not None and layer not in offline_layers:
                logger.info(
                    f"Skipping layer {layer.name()} :: not configured as offline layer"
                )
                continue

            subset_string = layer.subsetString()
            layer.setSubsetString("")

            datasource_hash = hashlib.sha256(
                layer.dataProvider().dataSourceUri().encode()
            ).hexdigest()

            datasource_mapping[datasource_hash].append(LayerInfo(layer, subset_string))

        for datasource_hash, layer_infos in datasource_mapping.items():
            layer_to_offline = layer_infos[0].layer
            self.create_layer(layer_to_offline, data_source, offline_gpkg_path)

        for datasource_hash, layer_infos in datasource_mapping.items():
            request = QgsFeatureRequest()
            # All layers for given `datasource_hash` are pointing to the very same file/datasource.
            # Here we get the first layer for convenience, but it doesn't really matter.
            layer_to_offline = layer_infos[0].layer

            if Qgis.QGIS_VERSION_INT >= 33000:
                no_geometry_types = [
                    Qgis.GeometryType.Null,
                    Qgis.GeometryType.Unknown,
                ]
            else:
                from qgis.core import QgsWkbTypes

                no_geometry_types = [
                    QgsWkbTypes.GeometryType.NullGeometry,
                    QgsWkbTypes.GeometryType.UnknownGeometry,
                ]

            # If `bbox` is valid and not empty and the layer is not geometry-less
            if (
                bbox
                and bbox.isFinite()
                and layer_to_offline.geometryType() not in no_geometry_types
            ):
                tr = QgsCoordinateTransform(
                    project.crs(), layer_to_offline.crs(), project
                )
                layer_bbox = tr.transform(bbox)
                request.setFilterRect(layer_bbox)

            source = self.convert_to_offline_layer(
                layer_to_offline, data_source, offline_gpkg_path, request
            )

            for layer_info in layer_infos:
                self.update_data_provider(layer_info.layer, source)
                layer_info.layer.setSubsetString(layer_info.subset_string)

        project_title = project.title()
        if not project_title:
            project_title = QFileInfo(project.fileName()).baseName()

        project_title += f"{project_title} (offline)"
        project.setTitle(project_title)
        project.writeEntry(
            PROJECT_ENTRY_SCOPE_OFFLINE,
            PROJECT_ENTRY_KEY_OFFLINE_DB_PATH,
            project.writePath(offline_gpkg_path),
        )
