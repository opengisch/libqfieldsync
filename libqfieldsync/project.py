from enum import Enum

from qgis.core import QgsProject

from libqfieldsync.utils.config_utils import Field, pfield


class QFieldItemSize(str, Enum):
    TINY = "tiny"
    NORMAL = "normal"
    BIG = "big"
    BIGGEST = "biggest"


class BaseMapType(str, Enum):
    SINGLE_LAYER = "singleLayer"
    MAP_THEME = "mapTheme"


class GeofencingBehavior(Enum):
    ALERT_INSIDE_AREAS = 1
    ALERT_OUTSIDE_AREAS = 2
    INFORM_ENTER_LEAVE_AREAS = 3


class InitialMapMode(str, Enum):
    BROWSE = "browse"
    DIGITIZE = "digitize"


class ProjectConfig:
    def __init__(self, project: QgsProject) -> None:
        self.project = project
        self.prefix = "qfieldsync"

    create_base_map = pfield(bool, "/createBaseMap", False)
    base_map_type = pfield(BaseMapType, "/baseMapType", BaseMapType.SINGLE_LAYER)
    base_map_theme = pfield(str, "/baseMapTheme")
    base_map_layer = pfield(str, "/baseMapLayer")
    digitizing_logs_layer = pfield(str, "/digitizingLogsLayer")
    initial_active_layer = pfield(str, "/initialActiveLayer")
    initial_map_mode = pfield(InitialMapMode, "/initialMapMode", InitialMapMode.BROWSE)
    geofencing_layer = pfield(str, "/geofencingLayer")
    geofencing_behavior = pfield(
        GeofencingBehavior, "/geofencingBehavior", GeofencingBehavior.ALERT_INSIDE_AREAS
    )
    geofencing_should_prevent_digitizing = pfield(
        bool, "/geofencingShouldPreventDigitizing", False
    )
    stamping_font_style = pfield(str, "/stampingFontStyle")
    stamping_horizontal_alignment = pfield(int, "/stampingHorizontalAlignment")
    stamping_image_decoration = pfield(str, "/stampingImageDecoration")
    stamping_details_template = pfield(str, "/stampingDetailsTemplate")
    force_stamping = pfield(bool, "/forceStamping", False)
    map_themes_active_layer = pfield(dict, "/mapThemesActiveLayers", dict)
    geofencing_is_active = pfield(bool, "/geofencingIsActive", False)
    maximum_image_width_height = pfield(int, "/maximumImageWidthHeight", 0)
    force_auto_push = pfield(bool, "/forceAutoPush", False)
    force_auto_push_interval_mins = pfield(int, "/forceAutoPushIntervalMins", 30)
    base_map_tile_size = pfield(int, "/baseMapTileSize", 1024)
    base_map_tiles_min_zoom_level = pfield(int, "/baseMapTilesMinZoomLevel", 14)
    base_map_tiles_max_zoom_level = pfield(int, "/baseMapTilesMaxZoomLevel", 14)
    offline_copy_only_aoi = pfield(bool, "/offlineCopyOnlyAoi", False)
    original_project_path = pfield(str, "/originalProjectPath", "")
    imported_files_checksums: Field["list[str]"] = pfield(
        list, "/importedFilesChecksums", list
    )
    layer_action_preference = pfield(str, "/layerActionPreference")
    area_of_interest = pfield(str, "/areaOfInterest")
    area_of_interest_crs = pfield(str, "/areaOfInterestCrs")
    feature_form_wizard_mode_enabled = pfield(
        bool, "/featureFormWizardModeEnabled", False
    )
    location_arrow_fill_color = pfield(str, "/locationArrowFillColor")
    location_arrow_outline_color = pfield(str, "/locationArrowOutlineColor")
    location_arrow_size = pfield(
        QFieldItemSize, "/locationArrowSize", QFieldItemSize.NORMAL
    )
    coordinate_cursor_fill_color = pfield(str, "/coordinateCursorFillColor")
    coordinate_cursor_outline_color = pfield(str, "/coordinateCursorOutlineColor")
    coordinate_cursor_size = pfield(
        QFieldItemSize, "/coordinateCursorSize", QFieldItemSize.NORMAL
    )
