"""Deterministic bounded schema inspection; never read application table rows."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

FORMAT = "sqlite-schema-lens/v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_OBJECTS = 10000
MAX_DETAILS = 100000
TYPES = {"table", "view", "index", "trigger"}


class LensError(ValueError):
    """An input cannot be inspected or interpreted."""


def _query(db, sql, args=()):
    cursor = db.execute(sql, args)
    return [dict(row) for row in cursor.fetchmany(MAX_DETAILS + 1)]


def inspect_database(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise LensError("Database must be an existing regular file.")
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        rows = db.execute("""SELECT type, name, tbl_name, sql FROM sqlite_schema
            WHERE type IN ('table','view','index','trigger') AND
              (substr(name,1,7) != 'sqlite_' OR
               (type = 'index' AND substr(name,1,17) = 'sqlite_autoindex_'))
            ORDER BY type, name COLLATE BINARY""").fetchmany(MAX_OBJECTS + 1)
        if len(rows) > MAX_OBJECTS:
            raise LensError("Database exceeds the schema object limit.")
        objects, detail_count = [], 0
        for row in rows:
            item = dict(row)
            details = {}
            if item["type"] in {"table", "view"}:
                details["columns"] = _query(db, 'SELECT cid,name,type,"notnull" AS not_null,dflt_value AS default_value,pk,hidden FROM pragma_table_xinfo(?) ORDER BY cid', (item["name"],))
                if item["type"] == "table":
                    details["foreign_keys"] = _query(db, 'SELECT id,seq,"table" AS target_table,"from" AS from_column,"to" AS to_column,on_update,on_delete,"match" AS "match" FROM pragma_foreign_key_list(?) ORDER BY id,seq', (item["name"],))
            if item["type"] == "index":
                details["columns"] = _query(db, 'SELECT seqno,cid,name,"desc" AS descending,coll AS collation,"key" AS is_key FROM pragma_index_xinfo(?) ORDER BY seqno', (item["name"],))
            detail_count += sum(len(v) for v in details.values())
            if detail_count > MAX_DETAILS:
                raise LensError("Database exceeds the schema detail limit.")
            item["details"] = details
            objects.append(item)
        db.rollback()
        result = {"format": FORMAT, "objects": objects}
        if len(encode_json(result).encode("utf-8")) > MAX_BYTES:
            raise LensError("Snapshot exceeds the byte limit.")
        return result
    except (sqlite3.Error, OSError, ValueError) as exc:
        if isinstance(exc, LensError):
            raise
        raise LensError("Could not read the SQLite schema. Check the database and its WAL permissions.") from exc
    finally:
        if db is not None:
            db.close()


def encode_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _object_pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise LensError("Snapshot contains a duplicate JSON key.")
        result[k] = v
    return result


def _validate_item(item):
    if not isinstance(item, dict) or set(item) != {"type", "name", "tbl_name", "sql", "details"}:
        raise LensError("Snapshot contains an invalid schema object.")
    if not isinstance(item["type"], str) or item["type"] not in TYPES or not isinstance(item["name"], str) or not item["name"] or not isinstance(item["tbl_name"], str):
        raise LensError("Snapshot contains an invalid schema object identity.")
    if item["sql"] is not None and not isinstance(item["sql"], str):
        raise LensError("Snapshot contains an invalid SQL definition.")
    detail_keys = {"table": {"columns", "foreign_keys"}, "view": {"columns"}, "index": {"columns"}, "trigger": set()}[item["type"]]
    details = item["details"]
    if not isinstance(details, dict) or set(details) != detail_keys:
        raise LensError("Snapshot contains invalid object details.")
    table_columns = {"cid", "name", "type", "not_null", "default_value", "pk", "hidden"}
    index_columns = {"seqno", "cid", "name", "descending", "collation", "is_key"}
    foreign_keys = {"id", "seq", "target_table", "from_column", "to_column", "on_update", "on_delete", "match"}
    for key, values in details.items():
        fields = foreign_keys if key == "foreign_keys" else index_columns if item["type"] == "index" else table_columns
        if not isinstance(values, list) or any(not isinstance(v, dict) or set(v) != fields for v in values):
            raise LensError("Snapshot contains invalid detail records.")
        numeric = {"id", "seq"} if key == "foreign_keys" else {"seqno", "cid", "descending", "is_key"} if item["type"] == "index" else {"cid", "not_null", "pk", "hidden"}
        nullable = {"to_column"} if key == "foreign_keys" else {"name", "collation"} if item["type"] == "index" else {"default_value"}
        seen = set()
        for value in values:
            if any((type(v) is not int if k in numeric else not isinstance(v, str) and not (v is None and k in nullable)) for k, v in value.items()):
                raise LensError("Snapshot contains unsupported detail values.")
            order_key = (value["id"], value["seq"]) if key == "foreign_keys" else value["seqno"] if item["type"] == "index" else value["cid"]
            if order_key in seen:
                raise LensError("Snapshot contains duplicate detail identities.")
            seen.add(order_key)


def validate_snapshot(obj):
    if not isinstance(obj, dict) or set(obj) != {"format", "objects"} or obj["format"] != FORMAT or not isinstance(obj["objects"], list):
        raise LensError("Unsupported or malformed snapshot format.")
    if len(obj["objects"]) > MAX_OBJECTS:
        raise LensError("Snapshot exceeds the schema object limit.")
    identities, details = set(), 0
    for item in obj["objects"]:
        _validate_item(item)
        identity = (item["type"], item["name"])
        if identity in identities:
            raise LensError("Snapshot contains duplicate object identities.")
        identities.add(identity)
        details += sum(len(v) for v in item["details"].values())
        if details > MAX_DETAILS:
            raise LensError("Snapshot exceeds the schema detail limit.")
    return obj


def read_snapshot(path: Path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise LensError("Snapshot exceeds the byte limit.")
        return validate_snapshot(json.loads(data.decode("utf-8-sig"), object_pairs_hook=_object_pairs,
                 parse_constant=lambda _: (_ for _ in ()).throw(LensError("Non-finite values are unsupported."))))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LensError("Could not read a valid UTF-8 JSON snapshot.") from exc


def compare(old, new):
    validate_snapshot(old)
    validate_snapshot(new)
    before = {(x["type"], x["name"]): x for x in old["objects"]}
    after = {(x["type"], x["name"]): x for x in new["objects"]}
    def identity(key):
        return {"type": key[0], "name": key[1]}
    added = [identity(k) for k in sorted(after.keys() - before.keys())]
    removed = [identity(k) for k in sorted(before.keys() - after.keys())]
    def canonical(item):
        result = dict(item)
        result["details"] = {key: sorted(value, key=(lambda v: (v["id"], v["seq"])) if key == "foreign_keys" else (lambda v: v["seqno"]) if item["type"] == "index" else (lambda v: v["cid"])) for key, value in item["details"].items()}
        return result
    changed = [identity(k) for k in sorted(before.keys() & after.keys()) if canonical(before[k]) != canonical(after[k])]
    return {"format": "sqlite-schema-lens-diff/v1", "equal": not (added or removed or changed),
            "added": added, "removed": removed, "changed": changed}
