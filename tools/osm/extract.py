"""Extracts a town's real buildings from OpenStreetMap for the scenery.

    python3 tools/osm/extract.py Kharkiv [--radius 3000] [--tolerance 1.0] [--min-area 25]
    python3 tools/osm/extract.py --at 36.23,49.99 --name test --radius 1000

Downloads the buildings and streets within `radius` metres of the town centre (geo.py's
TOWNS, or `--at lon,lat`) through the Overpass API, keeps the answer in
target/osm/<id>_<radius>_<query hash>.json (reused unless `--refresh`), and writes
assets/osm/<id>.ron:

- footprints in metres around the centre at real scale (x east, y north), counter-clockwise,
  without the closing point, with their courtyards (multipolygon inner rings, clockwise);
- outlines simplified: points closer than `tolerance` metres to the line of their neighbours
  go, footprints and courtyards under `min_area` square metres too;
- the scenery's building kind (House, Apartment, Tower, Church, Shed, Barn, Ruin), the wall
  height in metres (from `height`, else `building:levels`, else a default by kind and size)
  and the roof (Gable and Hip only on four-sided footprints without courtyards, never on
  churches, else Flat), as the scenery draws them (`src/scenery/osm.rs`);
- the streets and roads (motorways to residential streets, no service roads, tracks or
  tunnels) as polylines cut at the radius, simplified like the outlines, with a width by class.

The theater squeezes the distances between towns, not the towns: the scenery lays the
footprints around the town's position in the theater.

Prints an estimate of the triangles the scenery draws (walls and roofs, no floors), with the
town's ruined share of buildings in ruins, in all and within 2.5 km only (the small buildings),
for the rendering budget.

Data (c) OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright).
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools", "theater_ua"))

import geo  # noqa: E402

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USER_AGENT = "stealth-mission-89 tools/osm/extract.py"

FLOOR = 3.0

# OSM building values by scenery kind.
CHURCH = {"church", "cathedral", "chapel", "mosque", "synagogue", "temple", "monastery", "religious"}
HOUSE = {"house", "detached", "semidetached_house", "bungalow", "cabin", "farm", "terrace", "villa"}
APARTMENT = {"apartments", "residential", "dormitory", "hotel", "office", "commercial", "retail",
             "civic", "public", "government", "school", "university", "college", "hospital",
             "kindergarten", "train_station", "transportation", "supermarket"}
SHED = {"garage", "garages", "shed", "carport", "hut", "kiosk", "service", "toilets", "allotment_house"}
BARN = {"industrial", "warehouse", "factory", "manufacture", "hangar", "barn", "farm_auxiliary",
        "cowshed", "stable", "greenhouse", "storage_tank", "sports_hall", "stadium"}
RUIN = {"ruins", "destroyed", "collapsed"}
SKIP = {"no", "roof", "construction", "proposed", "demolished", "bridge", "canopy"}

# Road width (metres) by highway class.
ROAD_WIDTH = {
    "motorway": 22.0, "trunk": 16.0, "primary": 14.0, "secondary": 12.0, "tertiary": 10.0,
    "motorway_link": 7.0, "trunk_link": 7.0, "primary_link": 7.0, "secondary_link": 7.0,
    "tertiary_link": 7.0, "unclassified": 7.0, "residential": 7.0, "living_street": 6.0,
}

# The scenery's rules (src/scenery/osm.rs, content.rs): footprints drawn within 2.5 km only,
# the ruins' wall height cap.
SMALL_AREA = 150.0
RUIN_MAX_H = 18.0


def number(text):
    """Largest number of a tag value ("12", "12.5 m", "5;9" -> 9), metres for feet; None if none."""
    if text is None:
        return None
    values = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", text.replace(",", "."))]
    if not values:
        return None
    v = max(values)
    return v * 0.3048 if "'" in text or "ft" in text else v


def metres_per_degree(lat):
    """Metres per degree of longitude and of latitude at a latitude (WGS 84)."""
    p = math.radians(lat)
    return (111_412.84 * math.cos(p) - 93.5 * math.cos(3 * p),
            111_132.92 - 559.82 * math.cos(2 * p) + 1.175 * math.cos(4 * p))


def area(pts):
    """Signed area (positive counter-clockwise)."""
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))


def offset(p, a, b):
    """Distance from p to the line through a and b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    return abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / n


def simplify(pts, tolerance, closed=True):
    """Drops the point closest to the line of its neighbours while under `tolerance` (an open
    polyline keeps its ends)."""
    pts = list(pts)
    while len(pts) > (3 if closed else 2):
        best, at = tolerance, -1
        for i in range(0 if closed else 1, len(pts) if closed else len(pts) - 1):
            d = offset(pts[i], pts[i - 1], pts[(i + 1) % len(pts)])
            if d < best:
                best, at = d, i
        if at < 0:
            break
        del pts[at]
    return pts


def runs_inside(pts, radius):
    """The stretches of a polyline within `radius` of the centre."""
    runs, run = [], []
    for p in pts:
        if math.hypot(p[0], p[1]) <= radius:
            run.append(p)
            continue
        if len(run) >= 2:
            runs.append(run)
        run = []
    if len(run) >= 2:
        runs.append(run)
    return runs


def query(lon, lat, radius):
    around = f"(around:{radius},{lat},{lon})"
    return (
        f'[out:json][timeout:300];'
        f'(way["building"]{around};'
        f'relation["building"]["type"="multipolygon"]{around};'
        f'way["highway"~"^({"|".join(ROAD_WIDTH)})$"]{around};);'
        f'out geom;'
    )


def fetch(q, cache, refresh):
    """The Overpass answer to a query, from the cache or the first server that answers it
    whole (a `remark` means a cut-off answer)."""
    if os.path.exists(cache) and not refresh:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    data = urllib.parse.urlencode({"data": q}).encode()
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=360) as r:
                raw = r.read()
            answer = json.loads(raw)
        except (OSError, ValueError) as e:
            print(f"{url}: {e}", file=sys.stderr)
            continue
        if "remark" in answer:
            print(f"{url}: {answer['remark']}", file=sys.stderr)
            continue
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "wb") as f:
            f.write(raw)
        return answer
    raise SystemExit("Overpass unavailable")


def outlines(element):
    """Outer and inner rings of a way or multipolygon relation, as (lon, lat) lists."""
    if element["type"] == "way":
        return [[(n["lon"], n["lat"]) for n in element.get("geometry", [])]], []
    return rings(element, "outer"), rings(element, "inner")


def rings(element, role):
    """A multipolygon's rings of one role, as (lon, lat) lists."""
    done, open_ = [], []
    for m in element.get("members", []):
        if m.get("role") == role and m.get("geometry"):
            open_.append([(n["lon"], n["lat"]) for n in m["geometry"]])
    # Rings can be split over several ways: join them end to end.
    while open_:
        ring = open_.pop(0)
        joined = True
        while ring[0] != ring[-1] and joined:
            joined = False
            for i, w in enumerate(open_):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[-2::-1]
                else:
                    continue
                del open_[i]
                joined = True
                break
        done.append(ring)
    return done


def classify(tags, foot_area):
    """Scenery kind and wall height (metres)."""
    b = tags.get("building", "yes")
    height = number(tags.get("height"))
    levels = number(tags.get("building:levels"))
    roof_h = number(tags.get("roof:height"))
    if roof_h is None and number(tags.get("roof:levels")) is not None:
        roof_h = number(tags.get("roof:levels")) * FLOOR
    if b in RUIN or tags.get("ruins") == "yes":
        kind = "Ruin"
    elif b in CHURCH or tags.get("amenity") == "place_of_worship":
        kind = "Church"
    elif b in HOUSE:
        kind = "House"
    elif b in SHED:
        kind = "Shed"
    elif b in BARN:
        kind = "Barn"
    elif b in APARTMENT:
        kind = "Apartment"
    elif (levels or 0) <= 2 and (height or 0) <= 9 and foot_area < 250:
        kind = "House"
    elif (levels or 0) <= 3 and foot_area >= 1500:
        kind = "Barn"
    else:
        kind = "Apartment"
    if height is not None:
        h = height - (roof_h or 0.0)
    elif levels is not None:
        h = levels * FLOOR + 0.5
    else:
        h = {"House": 3.5, "Shed": 2.8, "Barn": 8.0, "Church": 14.0, "Ruin": 6.0}.get(kind)
        if h is None:
            # Untagged blocks: the larger the footprint, the more floors (Soviet five- and
            # nine-storey blocks).
            h = 15.5 if foot_area < 1200 else 27.5
    h = max(2.5, min(h, 300.0))
    if kind == "Apartment" and h >= 45.0:
        kind = "Tower"
    return kind, h


def roof(tags, kind, n, holes):
    """Gable or Hip on four-sided footprints without courtyards (built on their rectangle),
    never on churches (their outline), else Flat."""
    if n != 4 or holes or kind in ("Church", "Ruin"):
        return "Flat"
    shape = tags.get("roof:shape")
    if shape in ("gabled", "saltbox", "gambrel", "half-hipped"):
        return "Gable"
    if shape in ("hipped", "pyramidal"):
        return "Hip"
    if shape is None and kind in ("House", "Shed"):
        return "Gable"
    return "Flat"


def inside(p, pts):
    """Point in polygon (even-odd)."""
    hit = False
    for a, b in zip(pts, pts[1:] + pts[:1]):
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            hit ^= p[0] < x
    return hit


def enclosing_size(pts):
    """Length and width of the smallest rectangle around a polygon with a side along one of
    its edges (the scenery's building rectangle)."""
    best = (math.inf, 0.0, 0.0)
    for a, b in zip(pts, pts[1:] + pts[:1]):
        n = math.dist(a, b)
        if n < 1e-9:
            continue
        u = ((b[0] - a[0]) / n, (b[1] - a[1]) / n)
        xs = [p[0] * u[0] + p[1] * u[1] for p in pts]
        ys = [p[1] * u[0] - p[0] * u[1] for p in pts]
        sx, sy = max(xs) - min(xs), max(ys) - min(ys)
        if sx * sy < best[0]:
            best = (sx * sy, max(sx, sy), min(sx, sy))
    return best[1], best[2]


def triangles(rf, pts, holes):
    """Triangles the scenery draws: walls (2 per side) and the roof (no floor)."""
    n = len(pts)
    if rf != "Flat":
        # Walls of the rectangle; gable: two slopes and two end triangles, hip: two
        # trapezoids and two triangles.
        return 8 + 6
    # The outline's and courtyards' walls, the roof over the outline bridged to each courtyard.
    m = sum(len(h) for h in holes)
    return 2 * (n + m) + n + m + 2 * len(holes) - 2


def ruin_triangles(pts, h):
    """About the triangles of a ruin on the rectangle (content.rs `ruin`): wall panels inside
    and out (15% fallen), maybe a hanging slab, rubble mounds."""
    l, w = enclosing_size(pts)
    h = min(h, RUIN_MAX_H)
    panel = 4.0 if h > 8.0 else 3.0
    panels = 2 * (math.ceil(l / panel) + math.ceil(w / panel))
    return 0.85 * 4 * panels + (0.6 * 4 if h > 8.0 else 0) + 5 * (2 + int(l / 25))


def fmt(pts):
    return ", ".join(f"({x:.1f}, {y:.1f})" for x, y in pts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("town", nargs="?", help="a town of geo.py's TOWNS")
    ap.add_argument("--at", help="lon,lat of the centre instead of a town")
    ap.add_argument("--name", help="name (and file id) with --at")
    ap.add_argument("--radius", type=float, help="metres (default: the town's radius)")
    ap.add_argument("--tolerance", type=float, default=1.0, help="outline simplification, metres")
    ap.add_argument("--min-area", type=float, default=25.0, help="smallest footprint kept, square metres")
    ap.add_argument("--refresh", action="store_true", help="download again instead of the cached answer")
    ap.add_argument("--out", help="output file (default assets/osm/<id>.ron)")
    a = ap.parse_args()

    if a.at:
        lon, lat = (float(v) for v in a.at.split(","))
        name, radius, ruined = a.name or "osm", a.radius or 1000.0, 0.0
    else:
        town = next((t for t in geo.TOWNS if a.town and t[0].lower() == a.town.lower()), None)
        if town is None:
            raise SystemExit(f"unknown town {a.town!r}; towns: {', '.join(t[0] for t in geo.TOWNS)}")
        name, lon, lat, ruined = town[0], town[1], town[2], town[5]
        radius = a.radius or float(town[4])
    ident = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    q = query(lon, lat, radius)
    digest = hashlib.sha1(q.encode()).hexdigest()[:8]
    cache = os.path.join(ROOT, "target", "osm", f"{ident}_{int(radius)}_{digest}.json")
    out = a.out or os.path.join(ROOT, "assets", "osm", f"{ident}.ron")
    elements = fetch(q, cache, a.refresh)["elements"]

    kx, ky = metres_per_degree(lat)

    def project(p):
        return ((p[0] - lon) * kx, (p[1] - lat) * ky)

    roads = []
    for e in elements:
        tags = e.get("tags", {})
        width = ROAD_WIDTH.get(tags.get("highway"))
        if e["type"] != "way" or width is None or "building" in tags:
            continue
        if tags.get("tunnel") not in (None, "no") or tags.get("area") == "yes":
            continue
        pts = [project((n["lon"], n["lat"])) for n in e.get("geometry", [])]
        for run in runs_inside(pts, radius):
            roads.append((width, simplify(run, a.tolerance, closed=False)))

    buildings, dropped = [], 0
    for e in elements:
        tags = e.get("tags", {})
        if tags.get("building", "no") in SKIP or "building:part" in tags:
            continue
        outer, inner = outlines(e)
        courtyards = []
        for ring in inner:
            if len(ring) < 4 or ring[0] != ring[-1]:
                continue
            hole = simplify([project(p) for p in ring[:-1]], a.tolerance)
            if len(hole) >= 3 and abs(area(hole)) >= a.min_area:
                # Clockwise: the courtyard's walls face into it.
                courtyards.append(hole if area(hole) < 0 else hole[::-1])
        for ring in outer:
            if len(ring) < 4 or ring[0] != ring[-1]:
                dropped += 1
                continue
            pts = [project(p) for p in ring[:-1]]
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            if math.hypot(cx, cy) > radius:
                continue
            if area(pts) < 0:
                pts.reverse()
            pts = simplify(pts, a.tolerance)
            if len(pts) < 3 or area(pts) < a.min_area:
                dropped += 1
                continue
            holes = [c for c in courtyards if inside(c[0], pts)]
            foot = area(pts) + sum(area(c) for c in holes)
            kind, h = classify(tags, foot)
            buildings.append((kind, h, roof(tags, kind, len(pts), holes), pts, foot, holes))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"// {name}: OpenStreetMap buildings within {radius:.0f} m of the centre, from\n")
        f.write("// tools/osm/extract.py. Data (c) OpenStreetMap contributors, ODbL 1.0.\n")
        f.write("(\n")
        f.write(f'    name: "{name}",\n    lon: {lon},\n    lat: {lat},\n    radius: {radius:.1f},\n')
        f.write("    buildings: [\n")
        for kind, h, rf, pts, _, holes in buildings:
            extra = f", holes: [{', '.join(f'[{fmt(c)}]' for c in holes)}]" if holes else ""
            f.write(f"        (kind: {kind}, h: {h:.1f}, roof: {rf}, pts: [{fmt(pts)}]{extra}),\n")
        f.write("    ],\n")
        f.write("    roads: [\n")
        for width, pts in roads:
            f.write(f"        (width: {width:.1f}, pts: [{fmt(pts)}]),\n")
        f.write("    ],\n)\n")

    # Triangles: the ruins of the data, and the town's ruined share of the other buildings.
    near = total = 0.0
    for kind, h, rf, pts, foot, holes in buildings:
        ruin = ruin_triangles(pts, h)
        t = ruin if kind == "Ruin" else (1 - ruined) * triangles(rf, pts, holes) + ruined * ruin
        total += t
        if kind in ("House", "Shed") or foot < SMALL_AREA:
            near += t
    road_tris = sum(2 * (len(pts) - 1) for _, pts in roads)
    road_km = sum(math.dist(p, q) for _, pts in roads for p, q in zip(pts, pts[1:])) / 1000.0
    kinds = {}
    for b in buildings:
        kinds[b[0]] = kinds.get(b[0], 0) + 1
    tagged = [e["tags"] for e in elements if "building" in e.get("tags", {})]
    heights = sum(1 for t in tagged if "height" in t or "building:levels" in t)
    print(f"{name}: {len(buildings)} buildings ({dropped} dropped) within {radius:.0f} m -> {out}")
    print(f"  kinds: {', '.join(f'{k} {v}' for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))}")
    print(f"  height or levels tagged: {heights} of {len(tagged)} OSM buildings")
    print(f"  courtyards: {sum(len(b[5]) for b in buildings)} in {sum(1 for b in buildings if b[5])} buildings")
    print(f"  triangles, {ruined:.0%} ruined: about {total:.0f}, of which {near:.0f} within 2.5 km only")
    print(f"  roads: {len(roads)} ({road_km:.0f} km), {road_tris} triangles")
    print(f"  file: {os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
