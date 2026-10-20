"""Versioned seed -> immutable SQLite. Overlays remain session-local read inputs."""
import json
import hashlib
import math
import pathlib
import sqlite3
import tempfile
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parent
TABLES = ('pois', 'routes', 'tracks', 'documents', 'weather', 'contacts', 'visions', 'services')

def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, [a['lon'], a['lat'], b['lon'], b['lat']])
    x = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return round(6371000 * 2 * math.atan2(math.sqrt(x), math.sqrt(max(0, 1-x))), 1)

class Dataset:
    def __init__(self, seed_path=None, db_path=None):
        self.seed = json.loads(pathlib.Path(seed_path or ROOT/'seed-v1.json').read_text())
        Draft202012Validator(json.loads((ROOT/'seed-schema.json').read_text())).validate(self.seed)
        self.version = self.seed['dataset_version']
        self.seed_sha256=hashlib.sha256(json.dumps(self.seed,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        ids = {p['id'] for p in self.seed['pois']}
        for table in TABLES:
            rows = self.seed[table]
            if len({r['id'] for r in rows}) != len(rows):
                raise ValueError('duplicate dataset id: ' + table)
        for route in self.seed['routes']:
            if route['from_poi'] not in ids or route['to_poi'] not in ids:
                raise ValueError('broken route foreign key')
        for service in self.seed['services']:
            if service['poi_id'] not in ids:
                raise ValueError('broken service foreign key')
        if db_path is None:
            self.temp = tempfile.TemporaryDirectory(prefix='esa-data-')
            db_path = pathlib.Path(self.temp.name)/'seed.sqlite3'
        self.db_path = pathlib.Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS metadata (version TEXT, seed_sha256 TEXT)')
            previous = conn.execute('SELECT version, seed_sha256 FROM metadata').fetchone()
            if previous and (previous[0] != self.version or previous[1] != self.seed_sha256):
                raise ValueError('dataset version mismatch')
            if not previous:
                conn.execute('INSERT INTO metadata VALUES (?, ?)', (self.version,self.seed_sha256))
                for table in TABLES:
                    conn.execute('CREATE TABLE '+table+' (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
                    conn.executemany('INSERT INTO '+table+' VALUES (?, ?)', [(r['id'], json.dumps(r, ensure_ascii=False)) for r in self.seed[table]])
                conn.commit()
        finally:
            conn.close()

    def rows(self, table, overlays=None, *, identifier=None):
        if table not in TABLES:
            raise ValueError('unknown business table')
        conn = sqlite3.connect('file:'+str(self.db_path)+'?mode=ro', uri=True)
        try:
            sql, args = 'SELECT payload FROM '+table, ()
            if identifier is not None:
                sql += ' WHERE id = ?'
                args = (identifier,)
            rows = [json.loads(r[0]) for r in conn.execute(sql+' ORDER BY id', args)]
        finally:
            conn.close()
        patches = {r['id']:r for r in (overlays or {}).get(table, [])}
        return [dict(r, **patches.get(r['id'], {})) for r in rows]

    def validate_overlays(self, overlays):
        if not isinstance(overlays, dict):
            raise ValueError('data_overlays must be an object')
        for table, patches in overlays.items():
            if table not in TABLES or not isinstance(patches, list):
                raise ValueError('unknown overlay table or invalid rows')
            known = {r['id']: r for r in self.seed[table]}
            for patch in patches:
                if not isinstance(patch, dict) or patch.get('id') not in known:
                    raise ValueError('overlay must refer to a known id')
                if set(patch) - set(known[patch['id']]):
                    raise ValueError('unknown overlay field')
                for key, value in patch.items():
                    base = known[patch['id']][key]
                    if base is not None and (isinstance(value,bool)!=isinstance(base,bool) or (not isinstance(value,(int,float)) if isinstance(base,(int,float)) and not isinstance(base,bool) else not isinstance(value,type(base)))):
                        raise ValueError('overlay field type mismatch')
        return overlays

def relevance(query, row):
    text = json.dumps(row, ensure_ascii=False).lower()
    query = query.lower().strip()
    if not query:
        return 1
    terms = query.split()
    score = sum(len(t)*10 for t in terms if t in text)
    # Character bigrams give deterministic Chinese semantic keyword matching.
    score += sum(1 for i in range(len(query)-1) if query[i:i+2] in text)
    return score

def limit(value, default=5, maximum=100):
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int,float)) or int(value) != value or not 0 <= value <= maximum:
        raise ValueError('limit must be an integer between 0 and '+str(maximum))
    return int(value)
