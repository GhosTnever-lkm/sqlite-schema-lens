import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sqlite_schema_lens.core import LensError, compare, encode_json, inspect_database, read_snapshot
from sqlite_schema_lens.cli import main


class LensTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'data # ю.db'
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.executescript('''
                CREATE TABLE "parent""table" (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
                CREATE TABLE child (a INTEGER, b TEXT, generated TEXT GENERATED ALWAYS AS (b || '!') VIRTUAL,
                    FOREIGN KEY (a) REFERENCES "parent""table"(id));
                CREATE INDEX "idx ""quoted""" ON child(b);
                CREATE VIEW v AS SELECT b FROM child;
                CREATE TRIGGER tr AFTER INSERT ON child BEGIN SELECT 1; END;
            ''')

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_schema_objects_and_quoted_names(self):
        snap = inspect_database(self.db)
        objects = {(o["type"], o["name"]): o for o in snap["objects"]}
        self.assertIn(("table", 'parent"table'), objects)
        self.assertIn(("view", "v"), objects)
        self.assertIn(("trigger", "tr"), objects)
        self.assertIn(("index", 'idx "quoted"'), objects)
        columns = objects[("table", "child")]["details"]["columns"]
        self.assertTrue(any(c["name"] == "generated" and c["hidden"] for c in columns))
        self.assertEqual(objects[("table", "child")]["details"]["foreign_keys"][0]["target_table"], 'parent"table')

    def test_deterministic_comparison_and_change(self):
        first = inspect_database(self.db)
        self.assertEqual(encode_json(first), encode_json(inspect_database(self.db)))
        self.assertTrue(compare(first, first)["equal"])
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("ALTER TABLE child ADD COLUMN extra TEXT")
        diff = compare(first, inspect_database(self.db))
        self.assertFalse(diff["equal"])
        self.assertIn({"type": "table", "name": "child"}, diff["changed"])

    def test_snapshot_and_compare_cli(self):
        old, new = self.root / "old.json", self.root / "new.json"
        self.assertEqual(self.run_cli("snapshot", self.db, "--out", old)[0], 0)
        self.assertEqual(self.run_cli("compare", old, old)[0], 0)
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("CREATE TABLE added(id INTEGER)")
        self.assertEqual(self.run_cli("snapshot", self.db, "--out", new)[0], 0)
        code, output, _ = self.run_cli("compare", old, new, "--format", "json")
        self.assertEqual(code, 1)
        self.assertIn({"type": "table", "name": "added"}, json.loads(output)["added"])

    def test_missing_and_invalid_database(self):
        missing = self.root / "missing.db"
        self.assertEqual(self.run_cli("snapshot", missing, "--out", self.root / "out.json")[0], 2)
        self.assertFalse(missing.exists())
        corrupt = self.root / "bad.db"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaises(LensError):
            inspect_database(corrupt)

    def test_invalid_and_duplicate_snapshots(self):
        bad = self.root / "bad.json"
        for value in ['{}', '{"format":"x","format":"y","objects":[]}']:
            bad.write_text(value, encoding="utf-8")
            with self.assertRaises(LensError):
                read_snapshot(bad)

    def test_empty_object_names_round_trip(self):
        for kind, ddl in (
            ("table", 'CREATE TABLE "" (x TEXT);'),
            ("index", 'CREATE TABLE t (x TEXT); CREATE INDEX "" ON t(x);'),
        ):
            with self.subTest(kind=kind):
                db = self.root / (kind + ".db")
                with contextlib.closing(sqlite3.connect(db)) as con:
                    con.executescript(ddl)
                snapshot = self.root / (kind + ".json")
                self.assertEqual(self.run_cli("snapshot", db, "--out", snapshot)[0], 0)
                saved = read_snapshot(snapshot)
                self.assertTrue(any(o["type"] == kind and o["name"] == "" for o in saved["objects"]))
                self.assertTrue(compare(saved, saved)["equal"])
                self.assertEqual(self.run_cli("compare", snapshot, snapshot)[0], 0)

    def test_does_not_overwrite_inputs_or_sidecars(self):
        before = self.db.read_bytes()
        self.assertEqual(self.run_cli("snapshot", self.db, "--out", self.db)[0], 2)
        self.assertEqual(self.db.read_bytes(), before)
        sidecar = Path(str(self.db) + "-wal")
        self.assertEqual(self.run_cli("snapshot", self.db, "--out", sidecar)[0], 2)
        self.assertFalse(sidecar.exists())
        snap = self.root / "snapshot.json"
        self.assertEqual(self.run_cli("snapshot", self.db, "--out", snap)[0], 0)
        self.assertEqual(self.run_cli("compare", snap, snap, "--out", snap)[0], 2)

    def test_hardlink_output_alias_is_rejected(self):
        alias = self.root / "alias"
        try:
            alias.hardlink_to(self.db)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")
        self.assertEqual(self.run_cli("snapshot", self.db, "--out", alias)[0], 2)

    def test_symlink_database_cannot_overwrite_real_sidecar(self):
        alias = self.root / "database-link.db"
        try:
            alias.symlink_to(self.db)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links unavailable")
        sidecar = Path(str(self.db) + "-wal")
        self.assertEqual(self.run_cli("snapshot", alias, "--out", sidecar)[0], 2)
        self.assertFalse(sidecar.exists())

    def test_empty_database_and_snapshot_size_limit(self):
        empty = self.root / "empty.db"
        with contextlib.closing(sqlite3.connect(empty)):
            pass
        self.assertEqual(inspect_database(empty)["objects"], [])
        huge = self.root / "huge.json"
        huge.write_bytes(b" " * (8 * 1024 * 1024 + 1))
        with self.assertRaises(LensError):
            read_snapshot(huge)

    def test_corrupt_input_preserves_existing_snapshot(self):
        target = self.root / "out.json"
        target.write_text("previous", encoding="utf-8")
        broken = self.root / "broken.db"
        broken.write_text("not sqlite", encoding="utf-8")
        self.assertEqual(self.run_cli("snapshot", broken, "--out", target)[0], 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "previous")

    def test_live_wal_schema_is_seen(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            con.execute("CREATE TABLE wal_table(id INTEGER)")
            con.commit()
            self.assertTrue(any(x["name"] == "wal_table" for x in inspect_database(self.db)["objects"]))


if __name__ == "__main__":
    unittest.main()
