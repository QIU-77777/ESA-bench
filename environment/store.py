"""Single-record atomic session stores. MemoryStore is opt-in for tests only."""
import copy
import json
import threading
import redis

class MissingSession(Exception):
    pass

class Conflict(Exception):
    pass

class MemoryStore:
    def __init__(self, *, test_only=False):
        if not test_only:
            raise ValueError('memory store requires explicit test_only=True')
        self.records = {}
        self.lock = threading.RLock()

    def create(self, sid, value):
        with self.lock:
            if sid in self.records:
                raise Conflict('session already exists')
            self.records[sid] = copy.deepcopy(value)

    def read(self, sid):
        with self.lock:
            if sid not in self.records:
                raise MissingSession(sid)
            return copy.deepcopy(self.records[sid])

    def transact(self, sid, fn):
        with self.lock:
            value = self.read(sid)
            result = fn(value)
            self.records[sid] = value
            return copy.deepcopy(result)

    def delete(self, sid):
        with self.lock:
            return self.records.pop(sid, None) is not None

class RedisStore:
    def __init__(self, url='redis://redis:6379/0', ttl=86400, prefix='esa:session:', retries=16):
        if ttl <= 0 or retries <= 0:
            raise ValueError('ttl and retries must be positive')
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        self.client.ping()  # Fail closed: never switch to a memory store.
        self.ttl, self.prefix, self.retries = ttl, prefix, retries

    def create(self, sid, value):
        if not self.client.set(self.prefix + sid, json.dumps(value, ensure_ascii=False, allow_nan=False), ex=self.ttl, nx=True):
            raise Conflict('session already exists')

    def read(self, sid):
        raw = self.client.get(self.prefix + sid)
        if raw is None:
            raise MissingSession(sid)
        return json.loads(raw)

    def transact(self, sid, fn):
        key = self.prefix + sid
        for _ in range(self.retries):
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if raw is None:
                        raise MissingSession(sid)
                    record = json.loads(raw)
                    result = fn(record)
                    payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
                    pipe.multi()
                    pipe.set(key, payload, ex=self.ttl)
                    pipe.execute()
                    return result
                except redis.WatchError:
                    continue
        raise Conflict('concurrent transaction retry limit exceeded')

    def delete(self, sid):
        return bool(self.client.delete(self.prefix + sid))
