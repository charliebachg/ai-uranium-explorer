"""The data register must describe a usable, licensed, verified set of sources, or fail loudly."""

from __future__ import annotations

import pytest

from legacy_reader.prospect import inventory


@pytest.fixture(scope="module")
def inv():
    return inventory.load()


def test_the_real_inventory_loads(inv):
    assert inv.schema_version == "1.0.0"
    assert len(inv.sources) >= 15
    assert inv.region_bbox[0] < inv.region_bbox[2] and inv.region_bbox[1] < inv.region_bbox[3]


def test_every_source_names_a_licence_and_a_verification_date(inv):
    for s in inv.sources:
        assert s.licence.name, f"{s.key} has no licence name"
        assert s.verified_at, f"{s.key} has no verification date"


def test_the_labels_are_labels_and_nothing_else(inv):
    """Deposit footprints and occurrences are the positive class; using them as inputs would be leakage."""
    labels = {s.key for s in inv.with_role("label")}
    assert {"uranium_deposit_footprints", "smdi_uranium"} <= labels
    for key in labels:
        assert inv.by_key(key).role == "label"
        assert inv.by_key(key) not in inv.usable_features()


def test_the_strongest_athabasca_vector_is_present_and_carries_its_caveats(inv):
    em = inv.by_key("em_conductors")
    assert em.role == "feature" and em.bears_on == "pathway" and em.record_count == 28458
    assert em.verified is True
    # a conductor trace plots up-dip of blind ore, and silicification can hide it: both must be recorded
    text = " ".join(em.caveats).lower()
    assert "up-dip" in text and "resistor" in text
    assert em.fields_verified is False, "the attribute list is not confirmed; do not claim a conductance field"


def test_unverified_sources_are_not_offered_as_features(inv):
    assert inv.by_key("magnetic_domains").verified is False
    assert "magnetic_domains" not in {s.key for s in inv.usable_features()}
    assert inv.by_key("sentinel1_grd").verified is False


def test_imagery_carries_the_depth_caveat(inv):
    """No optical sensor sees a deposit under 1 to 2 km of sandstone, and the register has to say so."""
    caveats = " ".join(inv.by_key("sentinel2_l2a").caveats).lower()
    assert "never an ore detector" in caveats
    assert "100 to 900" in caveats or "sees them" in caveats


def test_the_read_tier_source_is_marked_unvalidated(inv):
    read_sources = [s for s in inv.sources if s.tier == "read"]
    assert read_sources, "the assay intervals read from reports are the one label no public layer supplies"
    caveats = " ".join(read_sources[0].caveats).lower()
    assert "unvalidated" in caveats
    assert "class-a" in caveats


def test_the_missing_datasets_are_recorded_with_evidence(inv):
    gaps = {g.key: g for g in inv.gaps}
    assert {"aeromagnetic_grids", "discovery_dates"} <= set(gaps)
    for g in inv.gaps:
        assert g.evidence and g.why_it_matters
    assert "interactive portal" in gaps["aeromagnetic_grids"].evidence.lower()
    assert "discoverytype is a method" in gaps["discovery_dates"].evidence.lower()


def test_no_source_names_the_banned_dispositions_service(inv):
    for s in inv.sources:
        assert inventory.BANNED_URL not in s.url


def test_source_and_preservation_are_not_claimed(inv):
    """Reports 01 and 02 give no Athabasca proxy for either, so the vocabulary does not allow the claim."""
    assert "source" not in inventory.BEARS_ON
    assert "preservation" not in inventory.BEARS_ON


def test_a_label_used_as_a_feature_is_refused(tmp_path):
    bad = tmp_path / "inv.toml"
    bad.write_text(
        'schema_version = "1.0.0"\nregion_bbox = [-112.0, 55.5, -101.5, 60.0]\n'
        '[licence.sk]\nname = "SK"\nterms = "open"\nredistributable = true\n'
        '[[source]]\nkey = "x"\ntitle = "X"\ntier = "native"\nrole = "nonsense"\n'
        'bears_on = "pathway"\naccess = "arcgis_rest"\nurl = "http://x"\nlicence = "sk"\n'
        'verified_at = "2026-01-01"\n'
    )
    with pytest.raises(inventory.InventoryError) as e:
        inventory.load(bad)
    assert "role" in str(e.value)


def test_a_source_without_a_licence_block_is_refused(tmp_path):
    bad = tmp_path / "inv.toml"
    bad.write_text(
        'schema_version = "1.0.0"\nregion_bbox = [-112.0, 55.5, -101.5, 60.0]\n'
        '[licence.sk]\nname = "SK"\nterms = "open"\nredistributable = true\n'
        '[[source]]\nkey = "x"\ntitle = "X"\ntier = "native"\nrole = "feature"\n'
        'bears_on = "pathway"\naccess = "arcgis_rest"\nurl = "http://x"\nlicence = "mystery"\n'
        'verified_at = "2026-01-01"\n'
    )
    with pytest.raises(inventory.InventoryError) as e:
        inventory.load(bad)
    assert "licence" in str(e.value)


def test_the_banned_service_is_refused_even_if_someone_adds_it(tmp_path):
    bad = tmp_path / "inv.toml"
    bad.write_text(
        'schema_version = "1.0.0"\nregion_bbox = [-112.0, 55.5, -101.5, 60.0]\n'
        '[licence.sk]\nname = "SK"\nterms = "open"\nredistributable = true\n'
        '[[source]]\nkey = "d"\ntitle = "Dispositions"\ntier = "native"\nrole = "context"\n'
        'bears_on = "study_area"\naccess = "arcgis_rest"\n'
        'url = "https://gis.saskatchewan.ca/arcgis/rest/services/Mining/MapServer/6"\nlicence = "sk"\n'
        'verified_at = "2026-01-01"\n'
    )
    with pytest.raises(inventory.InventoryError) as e:
        inventory.load(bad)
    assert "Mining/MapServer" in str(e.value)
