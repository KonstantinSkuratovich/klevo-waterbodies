"""Чтение выгрузок OpenStreetMap без внешних библиотек: `.osm` (XML) и `.osm.pbf` (спек 006, MAP-013, MAP-014).

Источник читается тремя проходами, чтобы не держать в памяти все узлы Беларуси (десятки миллионов):
`relations()` → `ways(...)` → `nodes(wanted)`. Теги отдаются словарём `str → str`.
"""
import struct
import xml.etree.ElementTree as ET
import zlib
from typing import Callable, Dict, Iterator, List, Set, Tuple

Tags = Dict[str, str]
Member = Tuple[str, int, str]  # (тип: node|way|relation, id, роль)

NANO = 1e-9
DEFAULT_GRANULARITY = 100
BLOB_HEADER_LIMIT = 64 * 1024
RELATION_TYPES = {0: 'node', 1: 'way', 2: 'relation'}


class OsmSource:
    """Общий интерфейс источника: три прохода по выгрузке."""

    def relations(self) -> Iterator[Tuple[int, Tags, List[Member]]]:
        raise NotImplementedError

    def ways(
        self, keep: Callable[[Tags], bool], extra_ids: Set[int]
    ) -> Iterator[Tuple[int, Tags, List[int]]]:
        """Линии: те, у которых `keep(tags)` истинно, и те, чей id в `extra_ids` (части мультиполигонов)."""
        raise NotImplementedError

    def nodes(self, wanted: Set[int]) -> Iterator[Tuple[int, float, float]]:
        raise NotImplementedError


def open_source(path: str) -> OsmSource:
    """Выбирает чтение по расширению: `.pbf` — PBF, остальное — XML."""
    return PbfSource(path) if path.lower().endswith('.pbf') else XmlSource(path)


# ───────────────────────────── XML ─────────────────────────────

class XmlSource(OsmSource):
    def __init__(self, path: str):
        self.path = path

    def _elements(self, tag: str):
        for _, el in ET.iterparse(self.path, events=('end',)):
            if el.tag == tag:
                yield el
                el.clear()

    @staticmethod
    def _tags(el) -> Tags:
        return {t.get('k'): t.get('v') for t in el.findall('tag')}

    def relations(self):
        for el in self._elements('relation'):
            members = [(m.get('type'), int(m.get('ref')), m.get('role') or '') for m in el.findall('member')]
            yield int(el.get('id')), self._tags(el), members

    def ways(self, keep, extra_ids):
        for el in self._elements('way'):
            way_id = int(el.get('id'))
            tags = self._tags(el)
            if way_id in extra_ids or keep(tags):
                yield way_id, tags, [int(nd.get('ref')) for nd in el.findall('nd')]

    def nodes(self, wanted):
        for el in self._elements('node'):
            node_id = int(el.get('id'))
            if node_id in wanted:
                yield node_id, float(el.get('lat')), float(el.get('lon'))


# ───────────────────────────── PBF ─────────────────────────────

def _varint(buf, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def _signed(n: int) -> int:
    """int64 в protobuf: отрицательные значения приходят как дополнение до 2^64."""
    return n - (1 << 64) if n >= 1 << 63 else n


def _zigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _fields(buf) -> Iterator[Tuple[int, int, object]]:
    """Поля сообщения protobuf: (номер, тип провода, значение). Для типа 2 значение — срез байт."""
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            length, pos = _varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
        elif wire == 1:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire == 5:
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f'неподдерживаемый тип провода protobuf: {wire}')
        yield field, wire, value


def _ints(value, wire: int) -> List[int]:
    """Повторяющееся целое поле: упаковано (тип 2) или одиночное значение (тип 0)."""
    if wire == 0:
        return [value]
    out = []
    pos = 0
    end = len(value)
    while pos < end:
        n, pos = _varint(value, pos)
        out.append(n)
    return out


def _deltas(numbers: List[int]) -> List[int]:
    total = 0
    out = []
    for n in numbers:
        total += _zigzag(n)
        out.append(total)
    return out


class PbfSource(OsmSource):
    def __init__(self, path: str):
        self.path = path

    def _blocks(self):
        """Распакованные PrimitiveBlock-и файла."""
        with open(self.path, 'rb') as fh:
            while True:
                head = fh.read(4)
                if len(head) < 4:
                    return
                (header_len,) = struct.unpack('>I', head)
                if header_len > BLOB_HEADER_LIMIT:
                    raise ValueError('повреждённый заголовок блока PBF')
                blob_type, data_size = '', 0
                for field, _, value in _fields(fh.read(header_len)):
                    if field == 1:
                        blob_type = bytes(value).decode()
                    elif field == 3:
                        data_size = value
                blob = fh.read(data_size)
                if blob_type != 'OSMData':
                    continue
                raw = None
                for field, _, value in _fields(blob):
                    if field == 1:
                        raw = bytes(value)
                    elif field == 3:
                        raw = zlib.decompress(value)
                if raw is not None:
                    yield raw

    @staticmethod
    def _block_parts(raw):
        strings: List[str] = []
        groups = []
        granularity, lat_off, lon_off = DEFAULT_GRANULARITY, 0, 0
        for field, _, value in _fields(raw):
            if field == 1:
                strings = [bytes(v).decode('utf-8', 'replace') for f, _, v in _fields(value) if f == 1]
            elif field == 2:
                groups.append(value)
            elif field == 17:
                granularity = value
            elif field == 19:
                lat_off = _signed(value)
            elif field == 20:
                lon_off = _signed(value)
        return strings, groups, granularity, lat_off, lon_off

    def relations(self):
        for raw in self._blocks():
            strings, groups, _, _, _ = self._block_parts(raw)
            for group in groups:
                for field, _, value in _fields(group):
                    if field != 4:
                        continue
                    rel_id, keys, vals, roles, memids, types = 0, [], [], [], [], []
                    for f, w, v in _fields(value):
                        if f == 1:
                            rel_id = v
                        elif f == 2:
                            keys += _ints(v, w)
                        elif f == 3:
                            vals += _ints(v, w)
                        elif f == 8:
                            roles += _ints(v, w)
                        elif f == 9:
                            memids += _ints(v, w)
                        elif f == 10:
                            types += _ints(v, w)
                    tags = {strings[k]: strings[x] for k, x in zip(keys, vals)}
                    members = [
                        (RELATION_TYPES.get(t, 'node'), mid, strings[r])
                        for t, mid, r in zip(types, _deltas(memids), roles)
                    ]
                    yield rel_id, tags, members

    def ways(self, keep, extra_ids):
        for raw in self._blocks():
            strings, groups, _, _, _ = self._block_parts(raw)
            for group in groups:
                for field, _, value in _fields(group):
                    if field != 3:
                        continue
                    way_id, keys, vals, refs = 0, [], [], []
                    for f, w, v in _fields(value):
                        if f == 1:
                            way_id = v
                        elif f == 2:
                            keys += _ints(v, w)
                        elif f == 3:
                            vals += _ints(v, w)
                        elif f == 8:
                            refs += _ints(v, w)
                    tags = {strings[k]: strings[x] for k, x in zip(keys, vals)}
                    if way_id in extra_ids or keep(tags):
                        yield way_id, tags, _deltas(refs)

    def nodes(self, wanted):
        for raw in self._blocks():
            strings, groups, granularity, lat_off, lon_off = self._block_parts(raw)
            for group in groups:
                for field, wire, value in _fields(group):
                    if field == 2:  # DenseNodes: идентификаторы и координаты — упакованные приращения
                        ids = lats = lons = None
                        for f, w, v in _fields(value):
                            if f == 1:
                                ids = _deltas(_ints(v, w))
                            elif f == 8:
                                lats = _ints(v, w)
                            elif f == 9:
                                lons = _ints(v, w)
                        if not ids or lats is None or lons is None:
                            continue
                        lat = lon = 0
                        for node_id, dlat, dlon in zip(ids, lats, lons):
                            lat += _zigzag(dlat)
                            lon += _zigzag(dlon)
                            if node_id in wanted:
                                yield node_id, (lat_off + granularity * lat) * NANO, (lon_off + granularity * lon) * NANO
                    elif field == 1:  # Node (без упаковки)
                        node_id = lat = lon = 0
                        for f, w, v in _fields(value):
                            if f == 1:
                                node_id = _zigzag(v)
                            elif f == 8:
                                lat = _zigzag(v)
                            elif f == 9:
                                lon = _zigzag(v)
                        if node_id in wanted:
                            yield node_id, (lat_off + granularity * lat) * NANO, (lon_off + granularity * lon) * NANO
