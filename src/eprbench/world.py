"""The world: one in-memory SQLite database plus a simulated clock.

Nothing in the package reads the wall clock. An episode's entire state is the
database, so snapshot/restore is a byte copy and resets cost microseconds.
"""

import re
import sqlite3
import threading
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Any

SCHEMA = resources.files("eprbench").joinpath("schema.sql").read_text()
SCHEMA_VERSION = "6"  # recorded in meta; a world saved at an earlier version (none recorded: step 2 and before) is migrated when restored


@lru_cache(maxsize=1 << 16)
def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(t: int | None) -> str | None:
    """A simulated time as ISO 8601 UTC. Cached: an episode formats the same few hundred times over and over."""
    return None if t is None else _iso(t)


def parse_iso(s: str) -> int:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _statements(script: str) -> list[str]:
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    return statements


@lru_cache(maxsize=1)
def _target() -> tuple[dict[str, list[tuple]], list[tuple[str, str, str]]]:
    """The current schema's tables (their columns as PRAGMA table_info gives them) and every object's CREATE statement."""
    conn = _connect()
    conn.executescript(SCHEMA)
    tables = {t: [tuple(c) for c in conn.execute(f'PRAGMA table_info("{t}")')] for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    created = []
    for s in _statements(SCHEMA):
        m = re.match(r"(?:\s*--[^\n]*\n)*\s*CREATE\s+(TABLE|VIEW|INDEX|TRIGGER)\s+\"?(\w+)\"?", s, re.IGNORECASE)
        if m:
            created.append((m[1].lower(), m[2], s))
    return tables, created


def migrate(conn: sqlite3.Connection, saved: str | None = None) -> None:
    """Bring a world saved by an earlier version (`saved`; None before versions were recorded) up to this schema, so
    saved evidence can be regraded and read: the columns and objects added since, filled as that version behaved (its
    samples were taken when requested, its rules were not gated on an encounter, a done job was done, every rule
    that took effect did so)."""
    tables, created = _target()
    have = {(r[0], r[1]) for r in conn.execute("SELECT type, name FROM sqlite_master")}
    for table, columns in tables.items():
        if ("table", table) not in have:
            continue
        present = {c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')}
        for _, name, kind, notnull, default, _ in columns:
            if name not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {name} {kind}' + (f" NOT NULL DEFAULT {default}" if notnull else f" DEFAULT {default}" if default else ""))
    for kind, name, sql in created:
        if (kind, name) not in have:
            conn.execute(sql)
    if saved is None:  # step 2
        conn.executescript(
            """UPDATE "order" SET collect_at = ordered_at, collected_at = ordered_at WHERE collect_at IS NULL;
               UPDATE job SET status = 'done' WHERE done_at IS NOT NULL AND status = 'open';
               INSERT INTO job_event (job_id, patient_id, at, status, by, note)
                 SELECT id, patient_id, created_at, 'open', created_by, NULL FROM job WHERE id NOT IN (SELECT job_id FROM job_event)
                 UNION ALL SELECT id, patient_id, done_at, 'done', done_by, outcome FROM job WHERE done_at IS NOT NULL AND id NOT IN (SELECT job_id FROM job_event);
               UPDATE scenario_rule SET encounter = NULL, unless = CASE WHEN unless IS NULL OR json_valid(unless) THEN unless ELSE json_array(unless) END;
               INSERT INTO scenario_application (rule_id, patient_id, event, applied_at)
                 SELECT p.rule_id, p.patient_id, p.event, p.applied_at FROM scenario_pending p WHERE p.applied_at IS NOT NULL
                 UNION ALL SELECT id, patient_id, '{}', applied_at FROM scenario_rule WHERE applied_at IS NOT NULL AND (on_op IS NULL OR delay_s = 0);
               INSERT OR IGNORE INTO meta (key, value) VALUES ('phlebotomy_rounds', '[]');"""
        )
    if saved in (None, "3"):  # step 3, first review: results and collections had no provenance; rules delivered late
        conn.executescript(
            """UPDATE scenario_rule SET after_discharge = 'legacy';
               UPDATE result SET origin = 'staff', origin_action_id = (SELECT o.origin_action_id FROM "order" o WHERE o.id = result.order_id)
                 WHERE order_id IS NOT NULL;
               INSERT INTO collection_event (order_id, patient_id, at, status, by, role, origin, origin_action_id)
                 SELECT id, patient_id, collected_at, 'collected', ordered_by, NULL, 'agent', origin_action_id FROM "order"
                 WHERE collected_at IS NOT NULL AND id NOT IN (SELECT order_id FROM collection_event);
               INSERT OR IGNORE INTO meta (key, value) VALUES ('observation_bounds', '{}');"""
        )
    if saved in (None, "3", "4"):  # step 4: read events, rebuilt from what each tool or API read returned (kept in the
        _read_events_from_audit(conn)  # audit log); a UI page's are not recoverable, so a saved UI run has none
    if saved == "5":  # a UI page's events then were what it rendered, which is not what the agent saw
        conn.execute("UPDATE read_event SET via = 'rendered' WHERE interface = 'ui'")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))


def saved_schema(snapshot: bytes) -> str | None:
    """The schema version a saved world was written at (None before versions were recorded), without migrating it."""
    conn = sqlite3.connect(":memory:")
    conn.deserialize(snapshot)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    return row[0] if row else None


class _Rows:
    """Enough of a World for ops.SHOWS to look up ids while a saved world is migrated."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.lock, self.now = conn, threading.RLock(), None

    def q(self, sql: str, params: Any = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur]


def _read_events_from_audit(conn: sqlite3.Connection) -> None:
    import json

    from eprbench import ops  # ops imports this module

    rows = _Rows(conn)
    found = conn.execute(
        f"""SELECT id, at, actor, interface, action_id, patient_id, op, result FROM audit WHERE ok = 1 AND result IS NOT NULL
            AND interface IN ('tool', 'api') AND op IN ({", ".join("?" * len(ops.SHOWS))}) ORDER BY id""",
        tuple(ops.SHOWS),
    ).fetchall()
    for audit_id, at, actor, interface, action_id, patient_id, name, result in found:
        rows.now = at
        try:
            ops.record_reads(rows, actor, interface, action_id, patient_id, name, json.loads(result), audit_id=audit_id)
        except (KeyError, TypeError, AttributeError, ValueError):  # a result in an older shape: what it showed is not known
            continue


class World:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys = ON")
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('now', 'schema')").fetchall())
        if meta.get("schema") != SCHEMA_VERSION:
            migrate(conn, meta.get("schema"))
        self._now = int(meta["now"])
        self.last_action: int | None = None  # the highest agent action id in the audit log, once read (see ops.next_action_id)

    @classmethod
    def create(cls, now: int = 0) -> "World":
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [("now", str(now)), ("schema", SCHEMA_VERSION)])
        return cls(conn)

    @classmethod
    def restore(cls, snapshot: bytes) -> "World":
        conn = _connect()
        conn.deserialize(snapshot)
        return cls(conn)

    def snapshot(self) -> bytes:
        return self.conn.serialize()

    @property
    def now(self) -> int:
        return self._now

    def set_time(self, now: int) -> None:
        self._now = now
        self.conn.execute("UPDATE meta SET value = ? WHERE key = 'now'", (str(now),))

    def advance(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("the clock cannot run backwards")
        self.set_time(self._now + seconds)

    def q(self, sql: str, params: Any = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params)]

    def one(self, sql: str, params: Any = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def insert(self, table: str, **cols: Any) -> int:
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        cur = self.conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', tuple(cols.values()))
        return cur.lastrowid
