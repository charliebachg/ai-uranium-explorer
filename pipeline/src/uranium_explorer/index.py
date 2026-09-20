"""`ue index pull`: provincial layers into data/index (raw GeoJSON or JSON, plus a pull log).

Every query uses a single-clause where (GeoDS WAF) and pages through maxRecordCount. Results are cached per
page by the ArcGIS client, so re-running is free and a partial run resumes.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .arcgis import ArcGisClient
from .paths import PATHS

GEODS = "https://geoscience-data-system.saskatchewan.ca/arcgis/rest/services"
EGIS = "https://gis.saskatchewan.ca/egis/rest/services"
NTS = "https://maps-cartes.services.geo.ca/server2_serveur2/rest/services/BaseMaps/NTS_SNRC/MapServer"

SK_LICENCE = "Government of Saskatchewan Standard Unrestricted Use Data Licence v2.0"
SK_LICENCE_URL = "https://gisappl.saskatchewan.ca/Html5Ext/Resources/GOS_Standard_Unrestricted_Use_Data_Licence_v2.0.pdf"
OGL_CANADA = "Open Government Licence - Canada"
OGL_CANADA_URL = "https://open.canada.ca/en/open-government-licence-canada"

# The web app's region of interest: the Athabasca Basin and its margins.
REGION_BBOX = (-112.0, 55.5, -101.5, 60.0)


#: an ArcGIS envelope filter on REGION_BBOX, for layers whose province-wide geometry dwarfs the study area
_REGION_CLIP: dict[str, Any] = {
    "geometry": ",".join(str(v) for v in REGION_BBOX),
    "geometryType": "esriGeometryEnvelope",
    "inSR": 4326,
    "spatialRel": "esriSpatialRelIntersects",
}


@dataclass(frozen=True)
class LayerPull:
    key: str
    url: str
    where: str
    out_fields: str
    geometry: bool
    fmt: str  # "geojson" or "json"
    title: str
    publisher: str
    licence: str
    licence_url: str | None
    redistributable: bool
    extra: dict[str, Any] | None = None


LAYERS: list[LayerPull] = [
    LayerPull(
        key="compilation",
        url=f"{EGIS}/Economy/Mineral_Exploration/FeatureServer/3",
        where="1=1",
        out_fields=",".join([
            "OBJECTID", "GOS_UNIQUE_DRILLHOLE_ID", "DRILLHOLE_NAME", "DATE_DRILLED", "COMMODITY_OF_INTEREST",
            "SOURCE", "COMPANY", "PROJECT_OR_PROPERTY_NAME", "EASTING_UTM", "NORTHING_UTM", "TOTAL_DH_LENGTH_M",
            "DH_INCLINATION", "DH_AZIMUTH", "DRILL_TYPE", "BASE_OF_ATHABASCA_SG_DEPTH_M",
            "TOP_CRYSTALLINE_BSMT_DEPTH_M",
        ]),
        geometry=True, fmt="geojson",
        title="Minerals and Quaternary Drillhole Compilation",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    LayerPull(
        key="geods_holes",
        url=f"{GEODS}/P_GeoDS_DrillingPage_EM/FeatureServer/2",
        where="SYS_SRC='DH'",
        out_fields=",".join([
            "OBJECTID", "HOLE_NAME", "TEMP_HOLE_ID", "TEMP_ASSMNT_FILE_NUM", "CMPNY_NAME", "PROPRTY_NAME",
            "DRILLNG_TYPE", "TOTL_MSRD_DPTH_M", "INCLNTN_DEG", "AZM_DEG", "DRILLNG_START_DATE", "NTS",
            "LAT_DEG", "LON_DEG", "ORIGNL_LAT_DEG", "ORIGNL_LON_DEG", "UTM_DATUM_TYPE", "UTM_PROJCTN_ZONE",
            "GROUND_ELEV_M", "LITHO_OBS", "GEOCHEM_ANLYSIS_EXST",
        ]),
        geometry=True, fmt="geojson",
        title="Saskatchewan Geoscience Data System: subsurface drilling (holes compiled from assessment files)",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence="No licence stated on the service", licence_url=None, redistributable=False,
    ),
    LayerPull(
        key="file_index_uranium",
        url=f"{GEODS}/P_GeoDS_AssessmentPage_EM/FeatureServer/20",
        where="CMMDTY LIKE '%ranium%'",
        out_fields=",".join([
            "OBJECTID", "ASSMNT_FILE_NUM", "SYSTEM_ASSMNT_FILE_NUM", "FILE_SIZE", "CMPNY", "PROPRTY_PROJCT",
            "WRK_PERIOD", "OFF_CNFNDL_DATE", "CMMDTY", "NTS_SHEET", "HSTRC_ASSMNT_WRK_DESCRPTN",
        ]),
        geometry=False, fmt="json",
        title="Saskatchewan Geoscience Data System: assessment file index (uranium-tagged)",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence="No licence stated on the service", licence_url=None, redistributable=False,
    ),
    *[
        LayerPull(
            key=f"assessment_info_{n}",
            url=f"{EGIS}/Economy/Mineral_Assessment_File_Information/FeatureServer/{n}",
            where="1=1",
            out_fields="OBJECTID,FILENUMBER,WORK_DATE,WORK_1,WORK_2,WORK_3,WORK_4,COMPANY,AREA_",
            geometry=False, fmt="json",
            title=f"Mineral Assessment File Information: {label}",
            publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
            licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
        )
        for n, label in ((0, "underground surveys"), (1, "ground surveys"), (2, "airborne surveys"))
    ],
    LayerPull(
        key="basin_geology",
        url=f"{EGIS}/Economy/Million_Scale_Geology/FeatureServer/6",
        where="GEOLOGICAL_REGION_1M='Athabasca Supergroup'",
        out_fields="OBJECTID,GEOLOGICAL_REGION_1M,ROCK_CODE_1M",
        geometry=True, fmt="geojson",
        title="Bedrock Geology 1:1,000,000 (Athabasca Supergroup polygons)",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    LayerPull(
        key="uranium_deposit_footprints",
        url=f"{EGIS}/Economy/Regional_Datasets_and_Compilations/FeatureServer/5",
        where="1=1",
        out_fields="*",
        geometry=True, fmt="geojson",
        title="Uranium Deposit Footprints",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    LayerPull(
        key="mineral_deposits_uranium",
        url=f"{EGIS}/Economy/Mineral_Exploration/FeatureServer/2",
        where="PRIMARYCOMMODITIES LIKE '%ranium%'",
        out_fields="OBJECTID,SMDI,NAME,PRIMARYCOMMODITIES,STATUS,SYMBOLOGY_STATUS,DISCOVERYTYPE,WEBLINK",
        geometry=True, fmt="geojson",
        title="Saskatchewan Mineral Deposit Index (uranium as primary commodity)",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    # ---------- prospect layers: the features a cell score is built from (see knowledge/data_inventory.toml)
    # The four map layers are clipped to the region box: province-wide 1:250,000 geometry is far larger than
    # the study area and nothing outside the basin and its margins is scored.
    LayerPull(
        key="em_conductors",
        url=f"{EGIS}/Economy/Regional_Datasets_and_Compilations/FeatureServer/7",
        where="1=1",
        out_fields="*",  # the attribute list is not documented; pull it all and record what arrives
        geometry=True, fmt="geojson",
        title="EM conductors (interpreted)",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
        extra=_REGION_CLIP,
    ),
    LayerPull(
        key="faults_250k",
        url=f"{EGIS}/Economy/250K_Scale_Geology/FeatureServer/2",
        where="1=1",
        out_fields="OBJECTID,FEAT_TYPE,FEAT_NAME,TYPE,DIRECTION,YEAR,CRIT_MAIN,SOURCE1,SOURCE2",
        geometry=True, fmt="geojson",
        title="Faults and lineaments, 1:250,000",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
        extra=_REGION_CLIP,
    ),
    LayerPull(
        key="bedrock_250k",
        url=f"{EGIS}/Economy/250K_Scale_Geology/FeatureServer/3",
        where="1=1",
        out_fields="OBJECTID,ROCK_CODE,EON,ERA,PERIOD,GROUP_,FORMATION,MEMBER,DOMAIN,LITHOLOGY,NAME",
        geometry=True, fmt="geojson",
        title="Bedrock geology, 1:250,000",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
        extra=_REGION_CLIP,
    ),
    LayerPull(
        key="surficial_250k",
        url=f"{EGIS}/Economy/250K_Scale_Geology/FeatureServer/1",
        where="1=1",
        out_fields="OBJECTID,CODE,MAIN_ENVIRONMENT",
        geometry=True, fmt="geojson",
        title="Surficial geology, 1:250,000",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
        extra=_REGION_CLIP,
    ),
    # survey footprints with geometry: where people actually looked, which is what the null model needs.
    # The attribute-only pulls above (assessment_info_*) drive file selection; these carry the polygons.
    *[
        LayerPull(
            key=f"survey_footprints_{label}",
            url=f"{EGIS}/Economy/Mineral_Assessment_File_Information/FeatureServer/{n}",
            where="1=1",
            out_fields="OBJECTID,FILENUMBER,WORK_DATE,WORK_1,COMPANY,AREA_",
            geometry=True, fmt="geojson",
            title=f"Mineral Assessment File Information: {label} survey footprints",
            publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
            licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
            extra=_REGION_CLIP,
        )
        for n, label in ((1, "ground"), (2, "airborne"))
    ],
    LayerPull(
        key="lake_sediment_gsc",
        url=f"{EGIS}/Economy/Analytical_and_Rock_Property_Data/FeatureServer/2",
        where="1=1",
        out_fields="OBJECTID,U,U_INA,TH_INA,LOI,LK_DEPTH,LK_AREA,GSC_OF,NTS_SHT",
        geometry=True, fmt="geojson",
        title="Lake sediment geochemistry (Geological Survey of Canada analyses)",
        publisher="Saskatchewan Geological Survey / Geological Survey of Canada",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    LayerPull(
        key="lake_sediment_sgs",
        url=f"{EGIS}/Economy/Analytical_and_Rock_Property_Data/FeatureServer/1",
        where="1=1",
        out_fields="OBJECTID,U_PPM,LOI_PERC,PB_PPM,NI_PPM,YR,SM,TYPE,NTS",
        geometry=True, fmt="geojson",
        title="Lake sediment geochemistry (provincial survey, 1975-1978)",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    LayerPull(
        key="lake_water_sgs",
        url=f"{EGIS}/Economy/Analytical_and_Rock_Property_Data/FeatureServer/0",
        where="1=1",
        out_fields="OBJECTID,U_PPM,PH,EH_MV,O2_PPM,COND_MICROSIEMENS_CM,LAKE_DEPTH,YR,TECHNIQUE,NTS",
        geometry=True, fmt="geojson",
        title="Lake water geochemistry",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    LayerPull(
        key="radioactive_boulders",
        url=f"{EGIS}/Economy/Regional_Datasets_and_Compilations/FeatureServer/4",
        where="1=1",
        out_fields="OBJECTID,YEAR,CPS,BACKGROUND,CPS_RANGE,LITHOLOGY,U308_ASSAY_RESULTS,ASSESSMENT_NUMBER",
        geometry=True, fmt="geojson",
        title="Radioactive boulders",
        publisher="Saskatchewan Geological Survey, Ministry of Energy and Resources",
        licence=SK_LICENCE, licence_url=SK_LICENCE_URL, redistributable=True,
    ),
    *[
        LayerPull(
            key=f"nts_{label}",
            url=f"{NTS}/{n}",
            where="1=1",
            out_fields="IDENTIF",
            geometry=True, fmt="geojson",
            title=f"National Topographic System grid ({label.upper()})",
            publisher="Natural Resources Canada",
            licence=OGL_CANADA, licence_url=OGL_CANADA_URL, redistributable=True,
            extra={
                "geometry": ",".join(str(v) for v in REGION_BBOX), "geometryType": "esriGeometryEnvelope",
                "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            },
        )
        for n, label in ((2, "250k"), (3, "50k"))
    ],
]

BANNED_URL_FRAGMENT = "Mining/MapServer"  # the mineral dispositions layer is never loaded (editorial rule)


def _assert_allowed(layer: LayerPull) -> None:
    if BANNED_URL_FRAGMENT in layer.url:
        raise ValueError(f"{layer.key}: the mineral dispositions service must never be queried")


def pull_layer(client: ArcGisClient, layer: LayerPull) -> dict[str, Any]:
    _assert_allowed(layer)
    expected = client.count(layer.url, layer.where) if not layer.extra else None
    features: list[dict[str, Any]] = []
    for page in client.iter_pages(layer.url, where=layer.where, out_fields=layer.out_fields,
                                  geometry=layer.geometry, fmt=layer.fmt, extra=layer.extra):
        features.extend(page.get("features", []))
    if expected is not None and len(features) != expected:
        raise RuntimeError(f"{layer.key}: fetched {len(features)} features but the service counts {expected}")
    if layer.fmt == "geojson":
        doc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    else:
        doc = {"features": [f.get("attributes", {}) for f in features]}
    out = PATHS.index / f"{layer.key}.{'geojson' if layer.fmt == 'geojson' else 'json'}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc))
    return {
        "key": layer.key, "url": layer.url, "where": layer.where, "count": len(features),
        "service_count": expected, "path": str(out.relative_to(PATHS.pipeline)),
        "title": layer.title, "publisher": layer.publisher, "licence": layer.licence,
        "licence_url": layer.licence_url, "redistributable": layer.redistributable,
        "retrieved_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }


def pull_all(keys: list[str] | None = None, log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    client = ArcGisClient(cache_dir=PATHS.cache / "arcgis")
    results = []
    log_path = PATHS.index / "pull_log.json"
    previous = {r["key"]: r for r in json.loads(log_path.read_text())} if log_path.is_file() else {}
    try:
        for layer in LAYERS:
            if keys and layer.key not in keys:
                continue
            r = pull_layer(client, layer)
            log(f"{layer.key}: {r['count']} features -> {r['path']}")
            previous[layer.key] = r
            results.append(r)
    finally:
        client.close()
        PATHS.index.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(list(previous.values()), indent=2))
    return results


def load_pull_log() -> dict[str, dict[str, Any]]:
    path = PATHS.index / "pull_log.json"
    return {r["key"]: r for r in json.loads(path.read_text())} if path.is_file() else {}


def read_features(key: str) -> list[dict[str, Any]]:
    for ext in ("geojson", "json"):
        p = PATHS.index / f"{key}.{ext}"
        if p.is_file():
            return json.loads(p.read_text())["features"]
    raise FileNotFoundError(f"{key} not pulled; run `ue index pull`")


def index_path(key: str) -> Path:
    return PATHS.index / key
