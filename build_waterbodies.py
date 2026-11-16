#!/usr/bin/env python3
"""Сборка справочника водоёмов Беларуси из выгрузки OpenStreetMap (спек 006, MAP-013).

  python waterbodies/build_waterbodies.py belarus-latest.osm.pbf \
      -o waterbodies/data/waterbodies.json --source "Geofabrik belarus-latest, 2026-11-10"

Отбор (Q-MAP-013-2): озёра, водохранилища, пруды и старицы с названием (сточные водоёмы, бассейны, шлюзы, каналы
не входят) и реки от 20 км с названием. Запись: код, название, тип, центр, область, площадь водоёма-полигона (`area_km2`, км²); водоёмы с центром вне границ областей (полоса соседних стран в выгрузке) не входят. Область — по границам областей
(admin_level=4, ISO3166-2) из той же выгрузки. Результат не зависит от порядка элементов в выгрузке: записи отсортированы,
координаты округлены до 5 знаков, поэтому повторный запуск даёт тот же файл. Только стандартная библиотека Python 3.10+.

Рядом пишутся упрощённые границы областей `data/oblasts.json` (MAP-014): допуск 10 м, файл ≈ 0,6 МБ; приложение определяет
по ним область точки без сети.

Данные © участники OpenStreetMap, лицензия ODbL 1.0 (https://www.openstreetmap.org/copyright): файл-результат —
производная база под той же лицензией (см. LICENSE-ODbL рядом со скриптом).
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import osm_reader  # noqa: E402

Point = Tuple[float, float]  # (lat, lon)
Ring = List[Point]

LICENSE = 'ODbL-1.0'
ATTRIBUTION = '© участники OpenStreetMap'
MIN_RIVER_KM = 20.0
RIVER_LINES = ('river', 'stream')
EARTH_KM = 6371.0088
COORD_DIGITS = 5
# Допуск упрощения границ областей (Рамера — Дугласа — Пекера), км: файл границ ≈ 0,6 МБ вместо ≈ 1,8 МБ при допуске
# 10 м на фоне точности OSM; соседние области упрощаются независимо, у общей границы возможна щель до двух допусков.
OBLAST_TOLERANCE_KM = 0.01
BAND_COUNT = 1024

# Подтипы полигонов `natural=water`: что входит в справочник. Без подтипа — обычный водоём («озеро»).
WATER_KINDS = {'lake': 'lake', 'reservoir': 'reservoir', 'pond': 'pond', 'oxbow': 'oxbow', 'river': 'river', '': 'lake'}

# Коды областей по ISO 3166-2; значения совпадают с `Oblast` в приложении (в нижнем регистре, как в правилах).
OBLAST_BY_ISO = {
    'BY-BR': 'brest', 'BY-VI': 'vitebsk', 'BY-HO': 'gomel', 'BY-HR': 'grodno',
    'BY-MI': 'minsk', 'BY-MA': 'mogilev', 'BY-HM': 'minsk_city',
}
# Город Минск лежит внутри Минской области: сначала проверяем его, остальные области не пересекаются.
OBLAST_ORDER = ['minsk_city', 'brest', 'vitebsk', 'gomel', 'grodno', 'minsk', 'mogilev']


def display_name(tags: Dict[str, str]) -> str:
    """Название для поиска: русское, если есть, иначе основное."""
    return (tags.get('name:ru') or tags.get('name') or '').strip()


def water_kind(tags: Dict[str, str]) -> Optional[str]:
    """Тип водоёма-полигона по тегам или `None`, если водоём в справочник не входит."""
    if not display_name(tags):
        return None
    if tags.get('landuse') == 'reservoir':
        return 'reservoir'
    if tags.get('waterway') == 'riverbank':
        return 'river'
    if tags.get('natural') == 'water':
        return WATER_KINDS.get(tags.get('water', ''))
    return None


def is_named_river_line(tags: Dict[str, str]) -> bool:
    """Река: линия `waterway=river`; в Беларуси многие реки в OSM помечены `stream`, поэтому он тоже считается (каналы — нет)."""
    return tags.get('waterway') in RIVER_LINES and bool(display_name(tags))


def is_water_relation(tags: Dict[str, str]) -> bool:
    return tags.get('type') == 'multipolygon' and water_kind(tags) is not None


def is_oblast_relation(tags: Dict[str, str]) -> bool:
    return tags.get('boundary') == 'administrative' and tags.get('admin_level') == '4' \
        and tags.get('ISO3166-2', '') in OBLAST_BY_ISO


# ───────────────────────────── геометрия ─────────────────────────────

def haversine_km(a: Point, b: Point) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


def assemble_rings(ways: List[List[int]]) -> List[List[int]]:
    """Склеивает линии по общим концам в замкнутые кольца (внешние контуры мультиполигона)."""
    rings: List[List[int]] = []
    open_parts = [list(w) for w in ways if len(w) >= 2]
    while open_parts:
        ring = open_parts.pop(0)
        while ring[0] != ring[-1]:
            for index, part in enumerate(open_parts):
                if part[0] == ring[-1]:
                    ring += part[1:]
                elif part[-1] == ring[-1]:
                    ring += part[-2::-1]
                elif part[-1] == ring[0]:
                    ring = part[:-1] + ring
                elif part[0] == ring[0]:
                    ring = part[:0:-1] + ring
                else:
                    continue
                del open_parts[index]
                break
            else:
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def ring_area_centroid(ring: Ring) -> Tuple[float, float, float]:
    """Площадь (км²) и центр масс контура в локальной проекции."""
    lat0 = sum(p[0] for p in ring) / len(ring)
    kx = math.cos(math.radians(lat0))
    area2 = cx = cy = 0.0
    for (la1, lo1), (la2, lo2) in zip(ring, ring[1:] + ring[:1]):
        x1, y1, x2, y2 = lo1 * kx, la1, lo2 * kx, la2
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if area2 == 0:
        return 0.0, ring[0][0], ring[0][1]
    km_per_deg = math.pi * EARTH_KM / 180
    return abs(area2) / 2 * km_per_deg ** 2, cy / (3 * area2), cx / (3 * area2) / kx


def point_in_ring(lat: float, lon: float, ring: Ring) -> bool:
    inside = False
    for (la1, lo1), (la2, lo2) in zip(ring, ring[1:] + ring[:1]):
        if (la1 > lat) != (la2 > lat) and lon < (lo2 - lo1) * (lat - la1) / (la2 - la1) + lo1:
            inside = not inside
    return inside


def center_of_ring(ring: Ring) -> Point:
    """Центр водоёма: центр масс; если он вне контура (старица, подкова) — середина самой длинной хорды по широте."""
    _, lat, lon = ring_area_centroid(ring)
    if point_in_ring(lat, lon, ring):
        return lat, lon
    crossings = []
    for (la1, lo1), (la2, lo2) in zip(ring, ring[1:] + ring[:1]):
        if (la1 > lat) != (la2 > lat):
            crossings.append((lo2 - lo1) * (lat - la1) / (la2 - la1) + lo1)
    crossings.sort()
    best = max(zip(crossings[0::2], crossings[1::2]), key=lambda c: c[1] - c[0], default=None)
    return (lat, (best[0] + best[1]) / 2) if best else ring[0]


def point_on_line(segments: List[Tuple[Point, Point]]) -> Point:
    """Точка на линии реки, ближайшая к центру масс её отрезков (длина как вес): лежит на самой реке."""
    total = cx = cy = 0.0
    for a, b in segments:
        length = haversine_km(a, b)
        total += length
        cy += length * (a[0] + b[0]) / 2
        cx += length * (a[1] + b[1]) / 2
    if total == 0:
        return segments[0][0]
    gl, go = cy / total, cx / total
    kx = math.cos(math.radians(gl))
    best, best_d = segments[0][0], float('inf')
    for a, b in segments:
        ax, ay, bx, by = a[1] * kx, a[0], b[1] * kx, b[0]
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((go * kx - ax) * dx + (gl - ay) * dy) / (dx * dx + dy * dy)))
        px, py = ax + t * dx, ay + t * dy
        dist = (px - go * kx) ** 2 + (py - gl) ** 2
        if dist < best_d:
            best_d, best = dist, (py, px / kx)
    return best


class Region:
    """Область: внешние контуры с индексом по полосам широты (быстрый поиск точки)."""

    def __init__(self, rings: List[Ring]):
        self.rings = rings
        lats = [p[0] for ring in rings for p in ring]
        self.min_lat, self.max_lat = min(lats), max(lats)
        self.min_lon = min(p[1] for ring in rings for p in ring)
        self.max_lon = max(p[1] for ring in rings for p in ring)
        self.step = (self.max_lat - self.min_lat) / BAND_COUNT or 1.0
        self.bands: Dict[int, List[Tuple[float, float, float, float]]] = defaultdict(list)
        for ring in rings:
            for (la1, lo1), (la2, lo2) in zip(ring, ring[1:] + ring[:1]):
                if la1 == la2:
                    continue
                low, high = sorted((la1, la2))
                for band in range(self._band(low), self._band(high) + 1):
                    self.bands[band].append((la1, lo1, la2, lo2))

    def _band(self, lat: float) -> int:
        return min(BAND_COUNT, max(0, int((lat - self.min_lat) / self.step)))

    def contains(self, lat: float, lon: float) -> bool:
        if not (self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon):
            return False
        inside = False
        for la1, lo1, la2, lo2 in self.bands.get(self._band(lat), ()):
            if (la1 > lat) != (la2 > lat) and lon < (lo2 - lo1) * (lat - la1) / (la2 - la1) + lo1:
                inside = not inside
        return inside


# ───────────────────────────── сборка ─────────────────────────────

def read_osm(source_path: str):
    """Три прохода по выгрузке; возвращает (водные отношения, области, линии, узлы)."""
    source = osm_reader.open_source(source_path)

    water_relations: Dict[int, Tuple[Dict[str, str], List[int]]] = {}
    oblast_relations: Dict[str, List[int]] = {}
    water_members: Set[int] = set()
    boundary_members: Set[int] = set()
    for rel_id, tags, members in source.relations():
        outer = [ref for kind, ref, role in members if kind == 'way' and role in ('outer', '')]
        if is_water_relation(tags):
            water_relations[rel_id] = (tags, outer)
            water_members.update(outer)
        elif is_oblast_relation(tags):
            oblast_relations[OBLAST_BY_ISO[tags['ISO3166-2']]] = outer
            boundary_members.update(outer)

    def wanted_way(tags: Dict[str, str]) -> bool:
        return is_named_river_line(tags) or water_kind(tags) is not None

    ways: Dict[int, Tuple[Dict[str, str], List[int]]] = {}
    for way_id, tags, refs in source.ways(wanted_way, water_members | boundary_members):
        ways[way_id] = (tags, refs)

    needed = {ref for _, refs in ways.values() for ref in refs}
    nodes: Dict[int, Point] = {}
    for node_id, lat, lon in source.nodes(needed):
        nodes[node_id] = (lat, lon)
    return water_relations, oblast_relations, ways, nodes, water_members


def oblast_rings(oblast_relations, ways, nodes) -> Dict[str, List[Ring]]:
    """Внешние контуры областей: код области → кольца из точек (широта, долгота)."""
    result = {}
    for code, outer in oblast_relations.items():
        parts = [ways[w][1] for w in outer if w in ways]
        rings = [[nodes[n] for n in ring if n in nodes] for ring in assemble_rings(parts)]
        rings = [r for r in rings if len(r) >= 4]
        if rings:
            result[code] = rings
    return result


def build_regions(oblast_relations, ways, nodes) -> Dict[str, Region]:
    return {code: Region(rings) for code, rings in oblast_rings(oblast_relations, ways, nodes).items()}


def simplify_ring(ring: Ring, tolerance_km: float) -> Ring:
    """Упрощение кольца методом Рамера — Дугласа — Пекера: точки дальше допуска от линии сохраняются."""
    lat0 = sum(p[0] for p in ring) / len(ring)
    km_per_deg = math.pi * EARTH_KM / 180
    flat = [(p[1] * math.cos(math.radians(lat0)) * km_per_deg, p[0] * km_per_deg) for p in ring]
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        first, last = stack.pop()
        ax, ay = flat[first]
        dx, dy = flat[last][0] - ax, flat[last][1] - ay
        length = math.hypot(dx, dy)
        far, far_index = -1.0, -1
        for i in range(first + 1, last):
            px, py = flat[i]
            dist = abs(dx * (ay - py) - (ax - px) * dy) / length if length else math.hypot(px - ax, py - ay)
            if dist > far:
                far, far_index = dist, i
        if far > tolerance_km:
            keep[far_index] = True
            stack += [(first, far_index), (far_index, last)]
    return [point for point, kept in zip(ring, keep) if kept]


def write_oblasts(rings_by_code: Dict[str, List[Ring]], output_path: str, source_note: str = '') -> int:
    """Пишет границы областей (упрощённые) в JSON для приложения; возвращает число точек."""
    oblasts = {}
    total = 0
    for code in sorted(rings_by_code):
        rings = [simplify_ring(r, OBLAST_TOLERANCE_KM) for r in rings_by_code[code]]
        rings = [r for r in rings if len(r) >= 4]
        # Порядок колец не зависит от порядка элементов выгрузки: крупные первыми, при равенстве — по первой точке.
        rings.sort(key=lambda r: (-len(r), r[0]))
        total += sum(len(r) for r in rings)
        oblasts[code] = [[round(c, COORD_DIGITS) for point in r for c in point] for r in rings]
    header = {'attribution': ATTRIBUTION, 'license': LICENSE, 'source': source_note,
              'tolerance_m': round(OBLAST_TOLERANCE_KM * 1000)}
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    lines = [f'{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},' for key, value in header.items()]
    body = ',\n'.join(f'{json.dumps(code)}: {json.dumps(rings, separators=(",", ":"))}'
                      for code, rings in oblasts.items())
    with open(output_path, 'w', encoding='utf-8', newline='\n') as out:
        out.write('{\n' + '\n'.join(lines) + '\n"oblasts": {\n' + body + '\n}\n}\n')
    return total


def oblast_of(regions: Dict[str, Region], lat: float, lon: float) -> Optional[str]:
    for code in OBLAST_ORDER:
        region = regions.get(code)
        if region and region.contains(lat, lon):
            return code
    return None


def polygon_records(water_relations, ways, nodes, water_members) -> Iterable[dict]:
    """Водоёмы-полигоны: замкнутые линии с тегами и мультиполигоны (внешние контуры; острова не учитываются)."""
    for way_id in sorted(ways):
        tags, refs = ways[way_id]
        kind = water_kind(tags)
        if kind is None or way_id in water_members or len(refs) < 4 or refs[0] != refs[-1]:
            continue
        ring = [nodes[n] for n in refs if n in nodes]
        if len(ring) >= 4:
            yield {'id': f'way/{way_id}', 'name': display_name(tags), 'kind': kind, 'ring': ring}
    for rel_id in sorted(water_relations):
        tags, outer = water_relations[rel_id]
        parts = [ways[w][1] for w in outer if w in ways]
        rings = [[nodes[n] for n in ring if n in nodes] for ring in assemble_rings(parts)]
        rings = [r for r in rings if len(r) >= 4]
        if rings:
            yield {'id': f'relation/{rel_id}', 'name': display_name(tags), 'kind': water_kind(tags),
                   'ring': max(rings, key=lambda r: ring_area_centroid(r)[0])}


def river_records(ways, nodes) -> Iterable[dict]:
    """Реки: линии `waterway=river` с одним названием — одна запись, если их общая длина от 20 км."""
    by_name: Dict[str, List[int]] = defaultdict(list)
    for way_id in sorted(ways):
        if is_named_river_line(ways[way_id][0]):
            by_name[display_name(ways[way_id][0])].append(way_id)
    for name in sorted(by_name):
        segments: List[Tuple[Point, Point]] = []
        for way_id in by_name[name]:
            line = [nodes[n] for n in ways[way_id][1] if n in nodes]
            segments += list(zip(line, line[1:]))
        if sum(haversine_km(a, b) for a, b in segments) >= MIN_RIVER_KM:
            yield {'id': f'river/{min(by_name[name])}', 'name': name, 'kind': 'river', 'point': point_on_line(segments)}


def build(source_path: str, output_path: str, source_note: str = '', oblasts_path: Optional[str] = None) -> int:
    """Собирает справочник из `.osm`/`.osm.pbf` в JSON; возвращает число записей.

    С `oblasts_path` рядом пишутся упрощённые границы областей (MAP-014) — из той же выгрузки, одним проходом.
    """
    water_relations, oblast_relations, ways, nodes, water_members = read_osm(source_path)
    rings_by_code = oblast_rings(oblast_relations, ways, nodes)
    regions = {code: Region(rings) for code, rings in rings_by_code.items()}
    if oblasts_path:
        write_oblasts(rings_by_code, oblasts_path, source_note)

    records = []
    river_names = set()
    for rec in river_records(ways, nodes):
        river_names.add(rec['name'])
        lat, lon = rec['point']
        records.append({**{k: rec[k] for k in ('id', 'name', 'kind')}, 'lat': lat, 'lon': lon})
    for rec in polygon_records(water_relations, ways, nodes, water_members):
        if rec['kind'] == 'river' and rec['name'] in river_names:
            continue  # у реки уже есть запись по линии
        lat, lon = center_of_ring(rec['ring'])
        # Площадь (км²) нужна карте, чтобы мелкие озёра подписывать только при сильном приближении (MAP-001).
        area_km2 = round(ring_area_centroid(rec['ring'])[0], 2)
        records.append({**{k: rec[k] for k in ('id', 'name', 'kind')}, 'lat': lat, 'lon': lon, 'area_km2': area_km2})

    for rec in records:
        rec['lat'] = round(rec['lat'], COORD_DIGITS)
        rec['lon'] = round(rec['lon'], COORD_DIGITS)
        rec['oblast'] = oblast_of(regions, rec['lat'], rec['lon'])
    # Выгрузка Geofabrik захватывает полосу соседних стран: центр вне границ областей — не Беларусь.
    records = [rec for rec in records if rec['oblast'] is not None]
    records.sort(key=lambda r: (r['name'].casefold(), r['id']))

    header = {'attribution': ATTRIBUTION, 'license': LICENSE, 'source': source_note}
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8', newline='\n') as out:
        out.write('{\n')
        for key, value in header.items():
            out.write(f'{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},\n')
        out.write('"waterbodies": [\n')
        out.write(',\n'.join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(',', ':')) for r in records))
        out.write('\n]\n}\n')
    return len(records)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Справочник водоёмов Беларуси из выгрузки OpenStreetMap.')
    parser.add_argument('source', help='выгрузка .osm.pbf или .osm')
    parser.add_argument('-o', '--output', default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                'data', 'waterbodies.json'))
    parser.add_argument('--oblasts-output', default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                  'data', 'oblasts.json'),
                        help='куда писать границы областей (MAP-014)')
    parser.add_argument('--source', dest='note', default='', help='откуда и когда выгрузка (попадает в файл)')
    args = parser.parse_args(argv)
    count = build(args.source, args.output, args.note, args.oblasts_output)
    print(f'записей: {count}, файл: {args.output}, границы областей: {args.oblasts_output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
