"""Surface fire behaviour physics.

Implements the standard models rather than tuned constants:

* **Rothermel (1972)** rate of spread, in the original US units, because every
  empirical coefficient (3.348, 133, 495, 0.0594, 0.792, …) is defined for
  sigma in ft^-1, wind in ft/min and loads in lb/ft^2. Mixing units silently
  scales the answer, so the computation is done natively and converted at the end.
* **Anderson (1982) fuel models 1/2/3/8/10** — the standard parameter sets.
* **Simard (1968)** equilibrium moisture content from air temperature and RH,
  used as the fine (1-h) dead fuel moisture.
* **Anderson (1983)** length-to-breadth ratio for the elliptical wavefront.
* **Byram (1959)** fireline intensity and flame length.

References
----------
Rothermel, R.C. (1972) A mathematical model for predicting fire spread in
    wildland fuels. USDA FS INT-115.
Anderson, H.E. (1982) Aids to determining fuel models for estimating fire
    behavior. USDA FS INT-122.
Anderson, H.E. (1983) Predicting wind-driven wildland fire size and shape.
    USDA FS INT-305.
Simard, A.J. (1968) Fire severity, news, and some related factors affecting
    fire danger. Quoted in the EMC comparison of Wang et al. (2024),
    GIScience & Remote Sensing 61(1).
Byram, G.M. (1959) Combustion of forest fuels. In *Forest Fire: Control and Use*.
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------- constants
LB_PER_TON_PER_ACRE = 2000.0 / 43560.0   # ton/acre -> lb/ft^2
FT_PER_M = 3.280839895
MIN_PER_SEC = 60.0

FUEL_PARTICLE_DENSITY = 32.0     # lb/ft^3 (Rothermel's rho_p)
HEAT_CONTENT = 8000.0            # BTU/lb (Rothermel's h)
TOTAL_MINERAL = 0.0555           # S_T
EFFECTIVE_MINERAL = 0.010        # S_e

#: Maximum length-to-breadth ratio used by FARSITE/BehavePlus (Anderson caps LB).
MAX_LB = 8.0

#: Fraction of the fine fuel load actually consumed in the flaming front.
CONSUMED_FRACTION = 0.8


def _t(load_tpa: float, sav_ft: float) -> tuple[float, float]:
    """(load lb/ft^2, surface-area-to-volume ratio ft^-1) for one size class."""
    return (load_tpa * LB_PER_TON_PER_ACRE, sav_ft)


#: Anderson (1982) fuel models. Dead classes only: this is a dead-fuel
#: Rothermel model, and live-fuel load transfer is deliberately out of scope.
#: (key -> name, dead classes, bed depth ft, moisture of extinction)
FUEL_MODELS: dict[str, dict] = {
    # 1 — short grass (annual, < 0.3 m)
    "short_grass": {"name": "Short grass (FM1)", "depth_ft": 1.0, "mx": 0.12,
                    "classes": (_t(0.74, 3500.0),)},
    # 2 — timber (grass and understory)
    "timber_grass": {"name": "Timber grass & understory (FM2)", "depth_ft": 1.0, "mx": 0.15,
                     "classes": (_t(2.0, 3000.0), _t(1.0, 109.0), _t(0.5, 30.0))},
    # 3 — tall grass (1.5 m)
    "tall_grass": {"name": "Tall grass (FM3)", "depth_ft": 2.5, "mx": 0.25,
                   "classes": (_t(3.01, 1500.0),)},
    # 8 — closed timber litter
    "timber_litter": {"name": "Closed timber litter (FM8)", "depth_ft": 0.2, "mx": 0.30,
                      "classes": (_t(1.5, 2000.0), _t(1.0, 109.0), _t(2.5, 30.0))},
    # 10 — timber litter and understory
    "timber_understory": {"name": "Timber litter & understory (FM10)", "depth_ft": 1.0, "mx": 0.25,
                          "classes": (_t(3.01, 2000.0), _t(2.0, 109.0), _t(5.01, 30.0))},
}

#: OSM land cover -> Anderson fuel model.
#:
#: `urban` is a deliberate, documented approximation: no standard surface-fuel
#: model describes structure-to-structure spread. Representing built-up ground by
#: the *closed timber litter* model with a reduced load factor keeps the surface
#: fire slower than in a forest, which is the conservative ordering for a product
#: whose warnings are about surface fire reaching settlements. It must not be read
#: as a model of building-to-building fire spread.
FUEL_TYPE_TO_MODEL = {
    "forest": "timber_understory",   # FM10 — the representative conifer stand
    "mixed": "timber_grass",         # FM2  — grass beneath an open canopy
    "field": "tall_grass",           # FM3
    "urban": "timber_litter",        # FM8  — the lowest-load surface fuel
}
#: Rothermel's model has no term for fuel discontinuity. In built-up ground the
#: surface fuel is patchy (roads, yards, paving), so the modelled spread rate is
#: multiplied by an explicit, documented continuity factor for `urban` only.
#: This is a surrogate for a missing process, not a Rothermel coefficient.
URBAN_CONTINUITY = 0.4
SHELTERED = {"forest", "mixed", "urban"}   # canopy / buildings shelter the wind


def fuel_model(fuel_type: str) -> str:
    return FUEL_TYPE_TO_MODEL.get((fuel_type or "").lower(), "timber_litter")


# ---------------------------------------------------------------- moisture
def emc_simard(rh_pct: float, temp_c: float) -> float:
    """Simard (1968) equilibrium moisture content, returned as a fraction.

    RH in percent, air temperature in degrees Celsius.
    """
    rh = max(1.0, min(float(rh_pct), 100.0))
    t = float(temp_c)
    if rh < 10.0:
        emc = 0.03 + 0.2626 * rh - 0.00104 * rh * t
    elif rh < 50.0:
        emc = 1.76 + 0.1601 * rh - 0.0266 * t
    else:
        emc = 21.06 - 0.4944 * rh + 0.005565 * rh * rh - 0.00063 * rh * t
    # physical bounds: dead fine fuel never dries below ~1% nor exceeds ~40%
    return max(0.01, min(emc / 100.0, 0.40))


# ---------------------------------------------------------------- wind
def wind_10m_to_20ft(wind_10m_ms: float) -> float:
    """Log-profile conversion of a 10 m wind to the 20 ft (6.1 m) reference."""
    if wind_10m_ms is None or wind_10m_ms <= 0:
        return 0.0
    return wind_10m_ms * (6.1 / 10.0) ** (1.0 / 7.0)


def midflame_wind(wind_10m_ms: float, fuel_type: str, waf_unsheltered: float = 0.4,
                  waf_sheltered: float = 0.15) -> float:
    """Midflame wind speed (m/s) from a 10 m wind.

    Wind adjustment factors follow the BehavePlus approach (Andrews 2012): roughly
    0.4 for unsheltered fuels and ~0.15 under a canopy.
    """
    waf = waf_sheltered if (fuel_type or "").lower() in SHELTERED else waf_unsheltered
    return wind_10m_to_20ft(wind_10m_ms) * waf


def length_to_breadth(wind_midflame_ms: float) -> float:
    """Anderson (1983) length-to-breadth ratio (wind in mi/h), capped at 8."""
    u_mph = max(0.0, float(wind_midflame_ms or 0.0)) * 2.2369362920544
    lb = 0.936 * math.exp(0.1147 * u_mph) + 0.461 * math.exp(-0.0692 * u_mph) - 0.397
    return max(1.0, min(lb, MAX_LB))


def eccentricity_from_lb(lb: float) -> float:
    """Ellipse eccentricity for a given length-to-breadth ratio."""
    lb = max(1.0, float(lb))
    return min(0.995, math.sqrt(max(0.0, 1.0 - 1.0 / (lb * lb))))


# ---------------------------------------------------------------- Rothermel
def rothermel_ros(fuel_type: str, wind_midflame_ms: float = 0.0, slope_deg: float = 0.0,
                  moisture: float = 0.08, load_factor: float = 1.0) -> float:
    """Rothermel (1972) no-wind/no-slope base rate of spread, m/min.

    `moisture` is the fine dead fuel moisture content as a fraction; `load_factor`
    scales the fuel load (used for the urban proxy).
    """
    key = fuel_model(fuel_type)
    model = FUEL_MODELS[key]
    classes = [(w * load_factor, s) for w, s in model["classes"]]
    depth_ft = model["depth_ft"]
    mx = model["mx"]

    w_total = sum(w for w, _ in classes)
    if w_total <= 0 or depth_ft <= 0:
        return 0.0

    # characteristic SAV and bulk density
    sigma = sum(w * s for w, s in classes) / w_total
    rho_b = w_total / depth_ft
    beta = rho_b / FUEL_PARTICLE_DENSITY

    beta_op = 3.348 * sigma ** -0.8189
    if beta_op <= 0:
        return 0.0
    ratio = beta / beta_op
    a_exp = 133.0 * sigma ** -0.7913
    gamma_max = sigma ** 1.5 / (495.0 + 0.0594 * sigma ** 1.5)
    gamma = gamma_max * ratio ** a_exp * math.exp(a_exp * (1.0 - ratio))

    # moisture and mineral damping
    r_m = min(moisture, mx) / mx if mx > 0 else 1.0
    eta_m = max(0.0, 1.0 - 2.59 * r_m + 5.11 * r_m ** 2 - 3.52 * r_m ** 3)
    eta_s = 0.174 * EFFECTIVE_MINERAL ** -0.19

    w_n = w_total / (1.0 + TOTAL_MINERAL)
    reaction_intensity = gamma * w_n * HEAT_CONTENT * eta_m * eta_s   # BTU/ft^2/min

    # propagating flux ratio
    xi = math.exp((0.792 + 0.681 * math.sqrt(sigma)) * (beta + 0.1)) / (192.0 + 0.2595 * sigma)

    # wind factor (U in ft/min — the coefficients require it)
    u_ft_min = max(0.0, float(wind_midflame_ms or 0.0)) * FT_PER_M * MIN_PER_SEC
    if u_ft_min > 0:
        c_w = 7.47 * math.exp(-0.133 * sigma ** 0.55)
        b_w = 0.02526 * sigma ** 0.54
        e_w = 0.715 * math.exp(-3.59e-4 * sigma)
        phi_w = c_w * u_ft_min ** b_w * ratio ** (-e_w)
    else:
        phi_w = 0.0

    # slope factor (tan^2 of the slope angle)
    tan_phi = math.tan(math.radians(max(0.0, min(float(slope_deg or 0.0), 60.0))))
    phi_s = 5.275 * beta ** -0.3 * tan_phi ** 2

    eps = math.exp(-138.0 / sigma)
    q_ig = 250.0 + 1116.0 * moisture          # BTU/lb
    denom = rho_b * eps * q_ig
    if denom <= 0:
        return 0.0

    ros_ft_min = reaction_intensity * xi * (1.0 + phi_w + phi_s) / denom
    return max(0.0, ros_ft_min * 0.3048)      # ft/min -> m/min


# ---------------------------------------------------------------- intensity
def byram_intensity(fuel_type: str, ros_m_min: float, load_factor: float = 1.0) -> float:
    """Byram (1959) fireline intensity, kW/m."""
    key = fuel_model(fuel_type)
    fine_load_lb_ft2 = FUEL_MODELS[key]["classes"][0][0] * load_factor
    fine_load_kg_m2 = fine_load_lb_ft2 * 4.88243           # lb/ft^2 -> kg/m^2
    consumed = fine_load_kg_m2 * CONSUMED_FRACTION
    ros_m_s = max(0.0, ros_m_min) / 60.0
    # H = 18608 kJ/kg
    return 18608.0 * consumed * ros_m_s                  # (kg/m^2)(m/s)(kJ/kg) = kW/m


def flame_length_m(intensity_kw_m: float) -> float:
    """Byram's flame length correlation, L = 0.0775 I^0.46 (m)."""
    return 0.0775 * max(0.0, intensity_kw_m) ** 0.46


def intensity_class(intensity_kw_m: float) -> str:
    """Byram's severity classes used by fire agencies."""
    if intensity_kw_m < 1000:
        return "low"
    if intensity_kw_m < 4000:
        return "moderate"
    if intensity_kw_m < 10000:
        return "high"
    return "extreme"


def spread_parameters(fuel_type: str, wind_10m_ms: float, slope_deg: float,
                      humidity_pct: float, temp_c: float) -> dict:
    """Everything the spread algorithm needs for one point, in SI units."""
    moisture = emc_simard(humidity_pct if humidity_pct is not None else 50.0,
                          temp_c if temp_c is not None else 20.0)
    is_urban = (fuel_type or "").lower() == "urban"
    continuity = URBAN_CONTINUITY if is_urban else 1.0
    w_mid = midflame_wind(wind_10m_ms, fuel_type)
    ros = rothermel_ros(fuel_type, w_mid, slope_deg, moisture, 1.0) * continuity
    lb = length_to_breadth(w_mid)
    # Byram intensity is a property of the flaming front, so it follows the
    # (reduced) spread rate but keeps the fuel load actually present.
    intensity = byram_intensity(fuel_type, ros, 1.0)
    return {
        "fuel_model": FUEL_MODELS[fuel_model(fuel_type)]["name"],
        "fuel_moisture": round(moisture, 4),
        "moisture_of_extinction": FUEL_MODELS[fuel_model(fuel_type)]["mx"],
        "wind_10m_ms": None if wind_10m_ms is None else round(float(wind_10m_ms), 2),
        "wind_midflame_ms": round(w_mid, 2),
        "ros_m_per_min": round(ros, 3),
        "continuity_factor": continuity,
        "length_to_breadth": round(lb, 2),
        "eccentricity": round(eccentricity_from_lb(lb), 4),
        "fireline_intensity_kw_m": round(intensity, 1),
        "flame_length_m": round(flame_length_m(intensity), 2),
        "intensity_class": intensity_class(intensity),
    }


# ---------------------------------------------------------------- zone metrics
def zone_metrics(poly, lat: float, lon: float) -> dict:
    """Geometric size of the burned zone: area, perimeter, diameter, head distance.

    Distances are computed in a local equirectangular projection, which is
    accurate to well under a percent at the scale of a single fire. The *diameter*
    is the greatest distance between any two points of the zone (the diameter of
    the convex hull), i.e. the longest straight line across the fire.
    """
    if poly is None or poly.is_empty:
        return {"area_ha": 0.0, "perimeter_km": 0.0, "diameter_m": 0.0, "head_distance_m": 0.0}

    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat)) or 1.0

    def to_local(x, y):
        return ((x - lon) * m_lon, (y - lat) * m_lat)

    try:
        coords = list(poly.exterior.coords)
    except Exception:
        return {"area_ha": 0.0, "perimeter_km": 0.0, "diameter_m": 0.0, "head_distance_m": 0.0}
    pts = [to_local(x, y) for x, y in coords]
    if len(pts) < 3:
        return {"area_ha": 0.0, "perimeter_km": 0.0, "diameter_m": 0.0, "head_distance_m": 0.0}

    # area in m^2 from the shoelace formula (works for the local projection)
    area_m2 = abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                      for i in range(len(pts)))) / 2.0
    perimeter_m = sum(math.dist(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))

    # convex hull diameter: brute force over hull vertices (hull stays small)
    hull_pts = _convex_hull_points(pts)
    diameter = 0.0
    for i in range(len(hull_pts)):
        for j in range(i + 1, len(hull_pts)):
            d = math.dist(hull_pts[i], hull_pts[j])
            if d > diameter:
                diameter = d

    head = max(math.hypot(px, py) for px, py in pts)
    return {"area_ha": round(area_m2 / 10000.0, 3), "perimeter_km": round(perimeter_m / 1000.0, 3),
            "diameter_m": round(diameter, 1), "head_distance_m": round(head, 1)}


def _convex_hull_points(pts):
    """Convex hull vertices of a point cloud, without importing shapely here."""
    # Andrew's monotone chain
    p = sorted(set(pts))
    if len(p) <= 2:
        return p

    def half(points):
        out = []
        for q in points:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (q[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (q[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(q)
        return out

    lower = half(p)
    upper = half(list(reversed(p)))
    return lower[:-1] + upper[:-1]
