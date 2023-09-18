import hashlib
from collections import defaultdict
from typing import List, Optional

from osgeo import ogr, osr
from PyQt5.QtCore import QFileInfo, QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsField,
    QgsFieldConstraints,
    QgsJsonUtils,
    QgsMapLayer,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    edit,
)

from .utils.logger import logger

CUSTOM_PROPERTY_IS_OFFLINE_EDITABLE = "isOfflineEditable"
CUSTOM_PROPERTY_REMOTE_SOURCE = "remoteSource"
CUSTOM_PROPERTY_REMOTE_PROVIDER = "remoteProvider"
CUSTOM_SHOW_FEATURE_COUNT = "showFeatureCount"
CUSTOM_PROPERTY_ORIGINAL_LAYERID = "remoteLayerId"
CUSTOM_PROPERTY_LAYERNAME_SUFFIX = "layerNameSuffix"
PROJECT_ENTRY_SCOPE_OFFLINE = "OfflineEditingPlugin"
PROJECT_ENTRY_KEY_OFFLINE_DB_PATH = "/OfflineDbPath"


def ogr_field_type(field: QgsField) -> ogr.FieldDefn:
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


def qgis_crs_to_ogr_srs(crs: QgsCoordinateReferenceSystem) -> osr.SpatialReference:
    """Converts a QGIS CRS to an OGR CRS."""
    auth_id = crs.authid()
    srs_wkt = crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED_GDAL)
    ogr_srs = osr.SpatialReference()

    if auth_id:
        ogr_srs.SetFromUserInput(auth_id)

    if ogr_srs.Validate() != 0:
        ogr_srs.SetFromUserInput(srs_wkt)

    return ogr_srs


def ogr_escape(data: str):
    # TODO work out how escaping actually works
    return data


def convert_to_offline_layer(
    layer: QgsVectorLayer,
    data_source: ogr.DataSource,
    offline_gpkg_path: str,
    feature_request: QgsFeatureRequest = QgsFeatureRequest(),
) -> str:
    """
    Will create a copy of ``layer`` in the GeoPackage specified as ``data_source`` which is stored at ``offline_gpkg_path``.
    It will replace the dataProvider of the original layer.
    """

    identifier = hashlib.sha256(
        layer.dataProvider().dataSourceUri().encode()
    ).hexdigest()

    layer_options = [
        "OVERWRITE=YES",
        f"IDENTIFIER={ogr_escape(identifier)}",
        f"DESCRIPTION={ogr_escape(layer.dataComment())}",
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

    ogr_srs = qgis_crs_to_ogr_srs(layer.crs())

    ogr_layer = data_source.CreateLayer(
        identifier, geom_type=layer.wkbType(), options=layer_options, srs=ogr_srs
    )

    fields = layer.dataProvider().fields()
    for field in fields:
        result = ogr_layer.CreateField(ogr_field_type(field))
        if result:
            raise RuntimeError(
                f"Creating field for {layer.name()}.{field.name()}({field.typeName()}) failed"
            )

    ogr_layer.SyncToDisk()
    qgis_uri = f"{offline_gpkg_path}|layername={identifier}"  # |option:QGIS_FORCE_WAL=ON' TODO: check why forcewal is enabled

    qgis_layer_options = QgsVectorLayer.LayerOptions(
        QgsProject.instance().transformContext()
    )

    new_layer = QgsVectorLayer(qgis_uri, identifier, "ogr", qgis_layer_options)

    if not new_layer.isValid():
        raise RuntimeError(f"We were not able to create the layer {layer.name()} ...")

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


def update_data_provider(layer: QgsVectorLayer, source: str) -> None:
    # Mark as offline layer
    layer.setCustomProperty(CUSTOM_PROPERTY_IS_OFFLINE_EDITABLE, True)

    # store original layer source and information
    layer.setCustomProperty(CUSTOM_PROPERTY_REMOTE_SOURCE, layer.source())
    layer.setCustomProperty(CUSTOM_PROPERTY_REMOTE_PROVIDER, layer.providerType())
    layer.setCustomProperty(CUSTOM_PROPERTY_ORIGINAL_LAYERID, layer.id())
    layer.setCustomProperty(CUSTOM_PROPERTY_LAYERNAME_SUFFIX, "")

    # TODO: review this code, why do we connect defaultvalueclause to not null constraints?
    #
    # remove constrainst of fields that use defaultValueClauses from provider on original
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
            if field.constraints().constraints() & QgsFieldConstraints.ConstraintUnique:
                layer.setFieldConstraint(index, QgsFieldConstraints.ConstraintUnique)

            # remove any undesired not null constraints coming from original data provider
            if field.name() in not_null_field_names:
                layer.removeFieldConstraint(
                    index, QgsFieldConstraints.ConstraintNotNull
                )


def convert_to_offline_project(
    offline_gpkg_path: str,
    layer_ids: Optional[List[str]],
    bbox: QgsRectangle = QgsRectangle(),
) -> None:
    """Converts the currently loaded QgsProject to an offline project.
    Offline layers are written to ``offline_gpkg_path``. Only valid vector layers are written.
    If ``layer_ids`` is specified, only layers present in this list are written.
    If ``bbox`` is specified, only features within this ``bbox`` are written.
    """
    project = QgsProject.instance()

    driver = ogr.GetDriverByName("GPKG")
    data_source = driver.CreateDataSource(offline_gpkg_path)

    # QgsOfflineEditing sets PRAGMA FOREIGN_KEY -- is this required?
    # QgsOfflineEditing calls Initialize Spatial Metadata -- is this required? Probably a spatialite leftover

    class LayerInfo:
        def __init__(self, layer, subset_string) -> None:
            self.layer = layer
            self.subset_string = subset_string

    # A dict that maps data sources (tables) to a list of layers connecting them
    datasource_mapping = defaultdict(list)
    for layer_id, layer in project.mapLayers().items():
        if layer.type() != QgsMapLayer.VectorLayer:
            logger.info(f"Skipping layer {layer.name()} :: not a vector layer")
            continue

        if not layer.isValid():
            reason = ""
            if layer.dataProvider():
                reason = layer.dataProvider().error()
            logger.info(f"Skipping layer {layer.name()} :: invalid ({reason})")
            continue

        if layer_ids is not None and layer_id not in layer_ids:
            logger.info(
                f"Skipping layer {layer.name()} :: not configured as offline layer"
            )
            continue

        subset_string = layer.subsetString()
        layer.setSubsetString("")

        provider_hash = hashlib.sha256(
            layer.dataProvider().dataSourceUri().encode()
        ).hexdigest()

        datasource_mapping[provider_hash].append(LayerInfo(layer, subset_string))

    for provider_hash, layers in datasource_mapping.items():
        request = QgsFeatureRequest()
        tr = QgsCoordinateTransform(project.crs(), layers[0].layer.crs(), project)
        if not bbox.isNull():
            layer_bbox = tr.transform(bbox)
            request.setFilterRect(layer_bbox)
        source = convert_to_offline_layer(
            layers[0].layer, data_source, offline_gpkg_path, request
        )  # TODO: take care of request (bbox)

        for layer in layers:
            update_data_provider(layer.layer, source)
            layer.layer.setSubsetString(layer.subset_string)

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
