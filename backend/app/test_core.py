"""Pure-logic tests: pytest backend/tests  (needs shapely + sqlalchemy; run inside the backend image)."""
import sys
import types

for m in ("cv2", "httpx", "PIL", "PIL.Image"):  # imported by services but not needed for these tests
    sys.modules.setdefault(m, types.ModuleType(m))

from shapely.geometry import Point

from app import services as sv
from app.models import Status, classify


def test_tiers():
    assert classify(0.39) == Status.CLEAR
    assert classify(0.40) == Status.PENDING_VERIFICATION
    assert classify(0.80) == Status.CRITICAL_ALERT


def test_ellipse_follows_wind():
    poly, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0)  # wind from the west: fire spreads east
    assert poly.contains(Point(92.9, 56.0))
    minx, miny, maxx, maxy = poly.bounds
    assert (maxx - 92.9) > 3 * (92.9 - minx)
    assert (maxx - minx) > (maxy - miny)


def test_circle_without_wind():
    poly, _ = sv.hazard_polygon(56.0, 92.9, None, None)
    minx, _, maxx, _ = poly.bounds
    assert abs((maxx - 92.9) - (92.9 - minx)) < 1e-3


def test_humidity_and_temp_shrink_the_zone():
    dry, _ = sv.hazard_polygon(56.0, 92.9, 5.0, 270.0, temp_c=33, humidity_pct=15)
    wet, _ = sv.hazard_polygon(56.0, 92.9, 5.0, 270.0, temp_c=10, humidity_pct=90)
    assert (dry.bounds[2] - dry.bounds[0]) > 2 * (wet.bounds[2] - wet.bounds[0])


def test_urban_fuel_is_smaller_than_forest():
    urban, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0, fuel="urban")
    forest, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0, fuel="forest")
    assert (urban.bounds[2] - urban.bounds[0]) < 0.85 * (forest.bounds[2] - forest.bounds[0])  # city burns slower than forest


def test_fuel_classification():
    city = {"places": [{"name": "C", "lat": 56.01, "lon": 92.9, "type": "city", "population": 900000}],
            "landuse": [{"kind": "residential", "lat": 56.005, "lon": 92.9}]}
    forest = {"places": [], "landuse": [{"kind": "forest", "lat": 56.01, "lon": 92.9}]}
    assert sv.classify_fuel(56.0, 92.9, city) == "urban"
    assert sv.classify_fuel(56.0, 92.9, forest) == "forest"
    assert sv.classify_fuel(56.0, 92.9, {"places": [], "landuse": []}) == "mixed"


def test_infra_kind_mapping():
    assert sv.infra_kind({"emergency": "fire_station"}) == "fire_station"
    assert sv.infra_kind({"amenity": "hospital"}) == "hospital"
    assert sv.infra_kind({"power": "substation"}) == "substation"  # Changed after refactoring
    assert sv.infra_kind({"amenity": "police"}) == "police"
    assert sv.infra_kind({"amenity": "cafe"}) == "other"


def test_infra_classify():
    # Test criticality classification
    kind, crit = sv.infra_classify({"power": "plant", "plant:source": "nuclear"})
    assert kind == "nuclear" and crit == 5

    kind, crit = sv.infra_classify({"amenity": "hospital"})
    assert kind == "hospital" and crit == 4

    kind, crit = sv.infra_classify({"amenity": "pharmacy"})
    assert kind == "pharmacy" and crit == 1

    kind, crit = sv.infra_classify({"amenity": "unknown"})
    assert kind == "other" and crit == 1


def test_evacuation_direction():
    poly, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0)
    places = [{"name": "East", "lat": 56.0, "lon": 92.95, "population": None},
              {"name": "North", "lat": 56.01, "lon": 92.9, "population": 500},
              {"name": "FarWest", "lat": 56.0, "lon": 92.5, "population": 900}]
    facilities = []  # empty facilities for backward compatibility test
    rows = {r["name"]: r for r in sv.evacuation(poly, 56.0, 92.9, places, 270.0)}
    # evacuation processes all places it receives, filtering is done by select_places
    assert set(rows) == {"East", "North", "FarWest"}
    assert rows["East"]["population"] is None
    assert rows["North"]["evac_bearing"] == 0  # cross-wind, away from the fire path


def test_evacuation_plan_with_facilities():
    poly, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0)
    places = [{"name": "East", "lat": 56.0, "lon": 92.95, "population": 1000}]
    facilities = [
        {"kind": "hospital", "name": "City Hospital", "lat": 56.001, "lon": 92.96, "criticality": 4},
        {"kind": "fire_station", "name": "Fire Dept", "lat": 56.002, "lon": 92.97, "criticality": 4}
    ]
    rows = sv.evacuation_plan(poly, 56.0, 92.9, places, facilities, 270.0)
    assert len(rows) == 1
    assert rows[0]["name"] == "East"
    assert "nearest_aid" in rows[0]
    assert len(rows[0]["nearest_aid"]) == 2
    # Check that nearest_aid contains the expected facility info
    assert rows[0]["nearest_aid"][0]["kind"] == "hospital"
    assert rows[0]["nearest_aid"][0]["name"] == "City Hospital"
