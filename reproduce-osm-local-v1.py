"""Independent OSM geographic change method for InAuction local-osm-v1.
No property identifiers, locations, job inputs, application logic or credentials.

Requires Python 3.12 and: pyvalhalla==3.9.3, osmium==4.3.2, shapely==2.2.0.
Use the verified index and graph/PBF archives:
https://github.com/valueproperties/inauction-open-geodata/releases/tag/osm-gr-2026-11-23
Input selection accepts only a public OSM node ID or a complete fixed 1-degree cell.
The geographic output is © OpenStreetMap contributors, ODbL 1.0.
https://opendatacommons.org/licenses/odbl/1-0/
"""
import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from unicodedata import normalize

from shapely import Point, STRtree, box
from shapely.affinity import scale
from shapely.geometry import shape
from shapely.ops import nearest_points

GRAPH_ARCHIVE_SHA256 = "eb05a0887e69c8e93f2ba508ef02d3cf88007f6c72f02d0b0b11680451b58b7f"
INDEX_ARCHIVE_SHA256 = "6f05ca4f80dd06760103ff1190d6e76bf67f0da994ae12f5ee8653052f1bf52e"
POLICY_SHA256 = "1c32e268fc597fe144311eb5d5a605105c17e072b0ae148e5618edcddb7dd33e"
REVISION = "413e2a9ae96bbad12e6e9c7ab69ab18cda18adb7208fc91eb2530a07d28814b3"
RoutingPoint = tuple[float, float]

REGISTRY = {'categories': {'METRO': {'mode': 'DYNAMIC', 'limitMeters': 2000}, 'TRAM': {'mode': 'DYNAMIC', 'limitMeters': 2000}, 'RAIL': {'mode': 'DYNAMIC', 'limitMeters': 5000}, 'BUS': {'mode': 'WALKING', 'limitMeters': 1000}, 'AIRPORT': {'mode': 'DRIVING', 'limitMeters': 30000}, 'PASSENGER_PORT': {'mode': 'DRIVING', 'limitMeters': 30000}, 'CARGO_PORT': {'mode': 'DRIVING', 'limitMeters': 30000}, 'MARINA': {'mode': 'DYNAMIC', 'limitMeters': 15000}, 'SEA': {'mode': 'STRAIGHT_LINE', 'limitMeters': 5000}, 'BEACH': {'mode': 'DYNAMIC', 'limitMeters': 10000}, 'SUPERMARKET': {'mode': 'WALKING', 'limitMeters': 2000}, 'SCHOOL': {'mode': 'DYNAMIC', 'limitMeters': 3000}, 'UNIVERSITY': {'mode': 'DYNAMIC', 'limitMeters': 5000}, 'HOSPITAL': {'mode': 'DRIVING', 'limitMeters': 20000}, 'PHARMACY': {'mode': 'WALKING', 'limitMeters': 2000}, 'CITY_CENTRE': {'mode': 'DYNAMIC', 'limitMeters': 15000}, 'SHOPPING_MALL': {'mode': 'DYNAMIC', 'limitMeters': 10000}, 'HIGHWAY': {'mode': 'DRIVING', 'limitMeters': 15000}, 'INDUSTRIAL_AREA': {'mode': 'DRIVING', 'limitMeters': 15000}, 'PARK_SQUARE': {'mode': 'WALKING', 'limitMeters': 2000}, 'PARKING_FACILITY': {'mode': 'WALKING', 'limitMeters': 1000}, 'HEALTH_CENTRE': {'mode': 'DYNAMIC', 'limitMeters': 20000, 'allowStraightLineFallback': True}}, 'excludedSourceFeatures': {'PASSENGER_PORT': {'n11430160557': {'reason': 'personnel_ferry', 'evidence': 'https://www.desfa.gr/wp-content/uploads/2025/09/E53_2025_TYDP-2024_2033.pdf'}}}}

MAX_SNAP_M = 35

def distance(a: RoutingPoint, b: RoutingPoint) -> float:
    x1, y1, x2, y2 = map(radians, (*a, *b))
    h = sin((y2-y1)/2)**2 + cos(y1)*cos(y2)*sin((x2-x1)/2)**2
    return 12742017.6 * asin(sqrt(min(1, h)))

@dataclass(frozen=True)
class Route:
    meters: float
    seconds: float
    reason: str | None = None

def endpoint_coordinates(shape: str) -> tuple[RoutingPoint, RoutingPoint]:
    offset = lat = lon = 0
    first = None
    while offset < len(shape):
        values = []
        for _ in range(2):
            n = shift = 0
            while True:
                c = ord(shape[offset])-63
                offset += 1
                n |= (c & 31) << shift
                shift += 5
                if c < 32:
                    break
            values.append((n >> 1) ^ -(n & 1))
        lat += values[0]
        lon += values[1]
        point = (lon/1e6, lat/1e6)
        if first is None:
            first = point
    if first is None:
        raise ValueError('empty_shape')
    return first, point

class Valhalla:
    def __init__(self, config: str, shortest: bool = False):
        import valhalla
        self.actor = valhalla.Actor(config)
        self.shortest = shortest

    @staticmethod
    def location(point):
        return dict(lon=point[0], lat=point[1], search_cutoff=MAX_SNAP_M)

    def options(self, mode):
        costing = {'WALKING': 'pedestrian', 'DRIVING': 'auto'}[mode]
        options = {'use_ferry': 0, 'speed_types': ['freeflow']}
        if self.shortest:
            options.update(shortest=True, disable_hierarchy_pruning=True)
        return dict(costing=costing, units='kilometers', costing_options={costing: options})

    def route(self, origin, target, mode, *, origin_cap=MAX_SNAP_M, end_cap=MAX_SNAP_M):
        query = dict(locations=[self.location(origin) | {'search_cutoff':origin_cap},
            self.location(target) | {'search_cutoff':end_cap}], **self.options(mode))
        try:
            trip = json.loads(self.actor.route(json.dumps(query)))['trip']
        except RuntimeError as exc:
            # Documented Valhalla no-edge, no-path or no-data errors only.
            if any(message in str(exc).lower() for message in (
                'no path', 'no suitable edges', 'no data found', 'no routable edge', 'failed to find path', 'unconnected regions'
            )):
                return Route(0, 0, 'no_route')
            return Route(0, 0, 'routing_error')
        try:
            maneuvers = [m for leg in trip['legs'] for m in leg['maneuvers']]
            if any(m.get('type') in (28, 29) or m.get('travel_type') == 'ferry' or m.get('portions_ferry') is True for m in maneuvers):
                return Route(0, 0, 'ferry_route')
            start = endpoint_coordinates(trip['legs'][0]['shape'])[0]
            finish = endpoint_coordinates(trip['legs'][-1]['shape'])[1]
            if distance(origin,start) > MAX_SNAP_M or distance(target,finish) > end_cap:
                return Route(0, 0, 'snap_too_far')
            return Route(trip['summary']['length']*1000, trip['summary']['time'])
        except (KeyError, ValueError, IndexError, TypeError):
            return Route(0, 0, 'unverifiable_route')

    def matrix(self, origin, targets, mode):
        """Preliminary estimates only. Individual routes are audited before acceptance."""
        if not targets:
            return []
        query = dict(sources=[self.location(origin)], targets=[self.location(p) for p in targets], **self.options(mode))
        try:
            rows = json.loads(self.actor.matrix(json.dumps(query)))['sources_to_targets'][0]
        except RuntimeError as exc:
            if 'unconnected regions' in str(exc).lower():
                return [self.route(origin, point, mode) for point in targets]
            return [Route(0, 0, 'routing_error') for _ in targets]
        return [Route(row['distance']*1000, row['time']) if row['time'] is not None
                else Route(0, 0, 'no_route') for row in rows]

REVIEWED_DUAL_SERVICE = {
    'n1039907394': {
        'nameEl': 'Γενικό Νοσοκομείο – Κέντρο Υγείας Νάξου',
        'nameEn': 'Naxos General Hospital and Health Centre',
        'source': 'https://naxoshospital.gov.gr/?page_id=21',
    },
}

def health_centre_no_route(origin, facilities, distance, limit):
    """Use only named eligible sites conclusively rejected as no_route.
    Access failures, technical errors and out-of-range routes never become fallbacks.
    """
    if not facilities or not all(item['reason'] == 'no_route' for item in facilities):
        return None
    ranked = []
    for item in facilities:
        lat, lon = item['mapPoint']
        meters = distance(origin, (lon, lat))
        if 0 < meters <= limit:
            ranked.append((meters, item['osmId'], item))
    if not ranked:
        return None
    meters, _, best = min(ranked, key=lambda row: row[:2])
    return {key: best[key] for key in ('osmId', 'mapPoint', 'nameEl', 'nameEn')} | {
        'mode': 'STRAIGHT_LINE', 'meters': meters, 'seconds': None, 'fallbackReason': 'no_route',
    }

def search_bounds(origin, max_m):
    lon, lat = origin
    # Conservative envelope for Greece, including maximum endpoint correlation.
    delta_y = (max_m*1.02 + 2*MAX_SNAP_M)/110_000
    delta_x = delta_y/cos(radians(abs(lat)+delta_y))
    return box(lon-delta_x, lat-delta_y, lon+delta_x, lat+delta_y)

def evaluate(provider, origin, rows, tree, key, entrance_modes=None, origin_cap=MAX_SNAP_M, *, dynamic_mode=None):
    rule = REGISTRY['categories'][key]
    limit = rule['limitMeters']
    xy = Point(origin)
    walk_calls = drive_calls = 0
    rejected = Counter()
    rejected_features = []
    candidates = []
    for index in tree.query(search_bounds(origin, limit)):
        ident, el, en, geometry = rows[index]
        if key == 'SEA':
            x_factor = cos(radians(origin[1]))
            bisector = scale(geometry, xfact=x_factor, yfact=1, origin=xy)
            match = nearest_points(xy, bisector)[1]
            destination = (origin[0]+(match.x-origin[0])/x_factor, match.y)
        else:
            destination = (geometry.x, geometry.y)
        geodesic = distance(origin, destination)
        lower = geodesic if key == 'SEA' else max(0, geodesic*.99 - 2*MAX_SNAP_M - 1)
        if lower <= limit:
            candidates.append((lower, ident, el, en, destination, geodesic))
    candidates.sort(key=lambda item:(item[0],item[1]))
    best = None
    unresolved = []
    examined = 0
    no_route = []
    exclusions = REGISTRY['excludedSourceFeatures'].get(key, {})
    for lower, ident, el, en, point, geodesic in candidates:
        if best and lower > best['meters']:
            break
        examined += 1
        exclusion = exclusions.get(ident)
        if exclusion:
            reason = exclusion['reason']
            rejected[reason] += 1
            rejected_features.append(dict(osmId=ident, reason=reason))
            continue
        mode = rule['mode']
        if mode == 'STRAIGHT_LINE':
            meters, seconds = geodesic, None
        else:
            if mode == 'DYNAMIC':
                mode = dynamic_mode or ('WALKING' if geodesic <= 2000 else 'DRIVING')
            if mode == 'WALKING':
                walk_calls += 1
            else:
                drive_calls += 1
            evidence = (entrance_modes or {}).get(ident)
            def calculate(chosen):
                if evidence is not None and chosen not in evidence:
                    return Route(0,0,'entrance_mode_not_evidenced')
                if isinstance(provider,Valhalla):
                    return provider.route(origin,point,chosen,origin_cap=origin_cap,end_cap=10 if evidence is not None else MAX_SNAP_M)
                return provider.route(origin,point,chosen)
            route = calculate(mode)
            if rule['mode'] == 'DYNAMIC' and mode == 'WALKING' and not route.reason and route.meters > 2000:
                if dynamic_mode == 'WALKING':
                    rejected['walking_limit'] += 1
                    continue
                mode = 'DRIVING'
                drive_calls += 1
                route = calculate(mode)
            if route.reason:
                if route.reason == 'no_route' and (el or en) and (evidence is not None or ident in REVIEWED_DUAL_SERVICE):
                    no_route.append(dict(osmId=ident,nameEl=el,nameEn=en,mapPoint=[point[1],point[0]],reason=route.reason))
                rejected[route.reason] += 1
                rejected_features.append(dict(osmId=ident, reason=route.reason))
                if route.reason in ('ferry_route', 'routing_error', 'unverifiable_route'):
                    unresolved.append(lower)
                continue
            meters, seconds = route.meters, route.seconds
        if meters <= 0 or meters > limit:
            reason = 'out_of_range' if meters > limit else 'zero_distance'
            rejected[reason] += 1
            rejected_features.append(dict(osmId=ident, reason=reason))
        elif best is None or (meters, ident) < (best['meters'], best['osmId']):
            best = dict(osmId=ident, nameEl=el, nameEn=en, meters=meters, seconds=seconds, mode=mode,
                        mapPoint=[point[1], point[0]])
    if not best and rule.get('allowStraightLineFallback') and candidates and len(no_route) == len(candidates):
        best = health_centre_no_route(origin,no_route,distance,limit)
    if unresolved and (not best or min(unresolved) <= best['meters']):
        best = None
        state = 'unverified_nearest'
    elif not rows:
        state = 'source_category_unverified'
    else:
        state = 'pilot_candidate' if best else 'no_eligible_point'
    reason = None
    if state == 'no_eligible_point':
        reason = 'none_in_source_window' if not candidates else 'candidate_rejected'
    if state == 'unverified_nearest':
        reason = 'unresolved_candidate_could_change_winner'
    if state == 'source_category_unverified':
        reason = 'no_eligible_features_in_extract'
    return dict(category=key, state=state, reason=reason, result=best, examined=examined,
                shortlist=len(candidates), rejected=dict(rejected), rejectedFeatures=rejected_features, routingCalls=walk_calls+drive_calls)

PUBLIC_INDEX_SHA256 = "15f26351eaa9a5fa9e37ed54c7fa094cb7947c8120afea7169052b1035fd8c2b"

PUBLIC_INDEX_ROWS = 51887

def public_index(path):
    with open(path, "rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != PUBLIC_INDEX_SHA256:
            raise ValueError("public_poi_index_checksum")
    rows = defaultdict(list)
    entrance_modes = {}
    count = 0
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line in source:
            feature = json.loads(line)
            tags = feature["properties"]
            category, osm_id = tags["category"], tags["osmId"]
            if category not in REGISTRY["categories"]:
                raise ValueError("unknown_public_poi_category")
            rows[category].append((osm_id, tags["nameEl"], tags["nameEn"], shape(feature["geometry"])))
            if tags["entranceModes"]:
                entrance_modes[osm_id] = set(tags["entranceModes"])
            count += 1
    if count != PUBLIC_INDEX_ROWS:
        raise ValueError("public_poi_index_count")
    return rows, entrance_modes

PBF_SHA = "7cc4114518d24dd85b0cd976a9a734223975b76fce3f16fe574e5f989e16d675"

COHORT_SIZE = 5_855_072

ROADS = {"residential","living_street","unclassified","tertiary","secondary","primary","pedestrian","footway"}

PUBLIC = {None,"yes","public","permissive","designated","official"}

GRID = .00025

def build(pbf, target):
    import osmium
    with pbf.open("rb") as f:
        if hashlib.file_digest(f,"sha256").hexdigest() != PBF_SHA:
            raise ValueError("public_pbf_checksum")
    if target.exists():
        with sqlite3.connect(target) as db:
            if db.execute("SELECT value FROM meta").fetchone() == (PBF_SHA,):
                return
            raise ValueError("different_cohort_exists")
    temp = target.with_suffix(".building")
    temp.unlink(missing_ok=True)
    db=sqlite3.connect(temp)
    try:
        db.executescript("""
          PRAGMA journal_mode=OFF;
          PRAGMA temp_store=FILE;
          CREATE TABLE grid(y INTEGER,x INTEGER,mode INTEGER,node INTEGER,lat REAL,lon REAL,
            PRIMARY KEY(y,x,mode)) WITHOUT ROWID;
          CREATE TABLE meta(value TEXT PRIMARY KEY);
          CREATE TABLE anchors(node INTEGER PRIMARY KEY,lat REAL,lon REAL,walk INTEGER,drive INTEGER);
          CREATE INDEX geographic_anchor ON anchors(lat,lon);
          CREATE TABLE routes(revision TEXT,node INTEGER,value TEXT,expires INTEGER,
            PRIMARY KEY(revision,node)) WITHOUT ROWID;
        """)
        sql="""INSERT INTO grid VALUES(?,?,?,?,?,?) ON CONFLICT(y,x,mode) DO UPDATE SET
            node=excluded.node,lat=excluded.lat,lon=excluded.lon
            WHERE excluded.node<grid.node"""
        pending=[]
        stream=osmium.FileProcessor(pbf,osmium.osm.NODE|osmium.osm.WAY)
        for way in stream.with_locations().with_filter(osmium.filter.KeyFilter("highway")):
            if not way.is_way():
                continue
            tags=way.tags
            if tags.get("highway") not in ROADS or tags.get("access") not in PUBLIC or "access:conditional" in tags:
                continue
            modes=[]
            if tags.get("foot") in PUBLIC and "foot:conditional" not in tags:
                modes.append(0)
            if tags.get("highway") not in {"pedestrian","footway"} and all(
                tags.get(k) in PUBLIC and k+":conditional" not in tags for k in ("vehicle","motor_vehicle","motorcar")):
                modes.append(1)
            for point in way.nodes:
                if not point.location.valid() or not (18<=point.lon<31 and 34<=point.lat<42):
                    continue
                pending.extend((math.floor(point.lat/GRID),math.floor(point.lon/GRID),mode,
                                point.ref,point.lat,point.lon) for mode in modes)
            if len(pending)>=20_000:
                db.executemany(sql,pending)
                db.commit()
                pending.clear()
        db.executemany(sql,pending)
        db.execute("INSERT INTO anchors SELECT node,min(lat),min(lon),max(mode=0),max(mode=1) FROM grid GROUP BY node")
        if db.execute("SELECT count(*) FROM anchors").fetchone() != (COHORT_SIZE,):
            raise ValueError("cohort_count_mismatch")
        db.execute("DROP TABLE grid")
        db.execute("INSERT INTO meta VALUES(?)",(PBF_SHA,))
        db.commit()
        db.execute("VACUUM")
    finally:
        db.close()
    temp.replace(target)

def route_revision(policy):
    return hashlib.sha256(f"local-osm-v1:{PBF_SHA}:{policy}".encode()).hexdigest()

def anchor_routes(item, rows_by_category, indexes, empty, provider, entrance_modes):
    point = (item["longitude"], item["latitude"])
    modes = [mode for mode,allowed in (("WALKING",item["walk"]),("DRIVING",item["drive"])) if allowed]
    results = []
    for category, rule in REGISTRY["categories"].items():
        available = modes if rule["mode"] == "DYNAMIC" else [rule["mode"]]
        for mode in available:
            if mode not in modes and mode != "STRAIGHT_LINE":
                continue
            row = evaluate(provider, point, rows_by_category.get(category, []), indexes.get(category, empty),
                           category, entrance_modes, dynamic_mode=mode if rule["mode"] == "DYNAMIC" else None)
            result = row["result"]
            if result and result["mode"] == mode:
                results.append({"category": category, **result})
    return results

def provider(tiles, index):
    if sum(1 for _ in tiles.rglob("*.gph")) != 513:
        raise ValueError("The pinned graph contains exactly 513 tiles.")
    program=shutil.which("valhalla_build_config")
    if not program:
        raise ValueError("Activate the pinned Valhalla environment.")
    output=subprocess.run([program,"--mjolnir-tile-dir",str(tiles.resolve()),"--mjolnir-concurrency","1"],
                          check=True,capture_output=True,text=True,timeout=20)
    settings=json.loads(output.stdout)
    for key in ("tile_extract","tile_url","traffic_extract"):
        settings["mjolnir"].pop(key,None)
    rows,modes=public_index(index)
    return settings,rows,modes


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--pbf",required=True,type=Path)
    cli.add_argument("--tiles",required=True,type=Path)
    cli.add_argument("--index",required=True,type=Path)
    cli.add_argument("--anchor-db",required=True,type=Path,help="Local path for the public cohort index")
    selectors=cli.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--node",type=int,help="Reproduce one public OSM node")
    selectors.add_argument("--cell",type=int,nargs=2,metavar=("WEST","SOUTH"),help="Reproduce a complete geographic cell")
    args=cli.parse_args()
    if args.cell and not (18 <= args.cell[0] < 31 and 34 <= args.cell[1] < 42):
        cli.error("Invalid geographic cell.")
    assert route_revision(POLICY_SHA256)==REVISION
    build(args.pbf,args.anchor_db)
    config,rows,entrances=provider(args.tiles,args.index)
    indexes={category:STRtree([r[3] for r in records]) for category,records in rows.items()}
    empty=STRtree([])
    with tempfile.TemporaryDirectory() as temp, sqlite3.connect(args.anchor_db) as db:
        config_path=Path(temp)/"valhalla.json"
        config_path.write_text(json.dumps(config))
        actor=Valhalla(str(config_path),shortest=False)
        if args.node is not None:
            query=db.execute("SELECT node,lat,lon,walk,drive FROM anchors WHERE node=?",(args.node,))
        else:
            west,south=args.cell
            query=db.execute("""SELECT node,lat,lon,walk,drive FROM anchors
                       WHERE lat>=? AND lat<? AND lon>=? AND lon<? ORDER BY node""",(south,south+1,west,west+1))
        print(json.dumps(dict(revision=REVISION,pbfSha256=PBF_SHA,indexSha256=PUBLIC_INDEX_SHA256,license="ODbL-1.0")))
        for node,lat,lon,walk,drive in query:
            points=anchor_routes(dict(latitude=lat,longitude=lon,walk=walk,drive=drive),
                                 rows,indexes,empty,actor,entrances)
            print(json.dumps(dict(osm_node=node,latitude=lat,longitude=lon,routes=points),ensure_ascii=False))


if __name__=="__main__":
    main()
