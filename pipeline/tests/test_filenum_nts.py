import json

import pytest

from uranium_explorer import nts
from uranium_explorer.filenum import display_file_num, norm_file_num, nts_from_file_num, parse_file_nums
from uranium_explorer.paths import PATHS


@pytest.mark.parametrize("raw,expected", [
    ("MAW2110", "MAW02110"),
    ("MAW02110", "MAW02110"),
    ("MAW 2110", "MAW02110"),
    ("maw2110", "MAW02110"),
    ("64L04-0105", "64L04-0105"),
    ("64L04-NW-0105", "64L04-0105"),
    ("64l04-0105", "64L04-0105"),
    ("74H-0012", "74H-0012"),
    ("63D14-0001 ", "63D14-0001"),
])
def test_norm_file_num(raw, expected):
    assert norm_file_num(raw) == expected


def test_display_file_num_round_trip():
    assert display_file_num("MAW02110") == "MAW2110"
    assert display_file_num("64L04-0105") == "64L04-0105"


def test_parse_file_nums_from_source_strings():
    assert parse_file_nums("74H14-0039") == ["74H14-0039"]
    assert parse_file_nums("MAW2143") == ["MAW02143"]
    assert parse_file_nums("64-D-04-NW-SF") == []
    assert parse_file_nums("RPT INV #6, P14") == []
    assert parse_file_nums(None) == []
    # a stamp and a note in one string, first appearance order, deduplicated
    assert parse_file_nums("see 64L04-NW-0075 and MAW 649; also 64L04-0075") == ["64L04-0075", "MAW00649"]


def test_nts_from_file_num():
    assert nts_from_file_num("64L04-0105") == "64L04"
    assert nts_from_file_num("74H-0012") == "74H"
    assert nts_from_file_num("MAW02110") is None


@pytest.mark.parametrize("sheet,bounds", [
    ("74H", (-106.0, 57.0, -104.0, 58.0)),
    ("74-H", (-106.0, 57.0, -104.0, 58.0)),
    ("64L04", (-104.0, 58.0, -103.5, 58.25)),
    ("64-L-04", (-104.0, 58.0, -103.5, 58.25)),
    ("064L04", (-104.0, 58.0, -103.5, 58.25)),
    ("74F08", (-108.5, 57.25, -108.0, 57.5)),
])
def test_nts_bounds(sheet, bounds):
    assert nts.nts_bounds(sheet) == pytest.approx(bounds)


def test_nts_norm_and_lists():
    assert nts.norm_nts("64-L-04") == "64L04"
    assert nts.parse_nts_list("63-D-14; 64-L-04") == ["63D14", "64L04"]
    assert nts.parse_nts_list("064L04; 064L04") == ["64L04"]
    assert nts.parse_nts_list("not a sheet") == []
    assert nts.parse_nts_list(None) == []


def test_nts_rejects_nonsense():
    with pytest.raises(nts.NtsError):
        nts.nts_bounds("74Z04")
    with pytest.raises(nts.NtsError):
        nts.nts_bounds("64L17")


def test_point_in_sheet():
    assert nts.point_in_sheet(-103.7, 58.1, "64L04")
    assert not nts.point_in_sheet(-105.0, 58.1, "64L04")


def test_bounds_match_the_pulled_nts_grid():
    """The computed grid must equal the NRCan layer wherever we have it on disk."""
    checked = 0
    for key in ("nts_250k", "nts_50k"):
        path = PATHS.index / f"{key}.geojson"
        if not path.is_file():
            continue
        for f in json.loads(path.read_text())["features"]:
            geom = f["geometry"]
            rings = geom["coordinates"] if geom["type"] == "Polygon" else [r for poly in geom["coordinates"] for r in poly]
            xs = [c[0] for ring in rings for c in ring]
            ys = [c[1] for ring in rings for c in ring]
            assert nts.nts_bounds(f["properties"]["IDENTIF"]) == pytest.approx(
                (min(xs), min(ys), max(xs), max(ys)), abs=0.01)
            checked += 1
    if not checked:
        import pytest as _pytest

        _pytest.skip("NTS layers not pulled")
