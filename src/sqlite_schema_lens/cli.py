"""CLI for bounded snapshots and schema comparison."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from . import __version__
from .core import LensError, compare, encode_json, inspect_database, read_snapshot


def _same_file(a: Path, b: Path) -> bool:
    if a.resolve() == b.resolve():
        return True
    try:
        return a.samefile(b)
    except (OSError, ValueError):
        return False


def _check_output(target: Path, inputs: list[Path]) -> None:
    if any(_same_file(target, source) for source in inputs):
        raise LensError("Output must not overwrite an input or a SQLite sidecar.")
    if not target.parent.is_dir() or target.is_dir():
        raise LensError("Output requires an existing parent directory and a file path.")


def _write_atomic(path: Path, contents: str) -> None:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".schema-lens-", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise LensError("Could not write the output file.") from exc
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _text_diff(report: dict) -> str:
    lines = ["Schemas match." if report["equal"] else "Schemas differ."]
    for key, label in (("added", "+"), ("removed", "-"), ("changed", "~")):
        for item in report[key]:
            lines.append(f'{label} {item["type"]} {json.dumps(item["name"], ensure_ascii=True)}')
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inspect SQLite schema and compare snapshots without reading table rows.")
    parser.add_argument("--version", action="version", version=f"sqlite-schema-lens {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="Create a JSON schema snapshot")
    snap.add_argument("database", type=Path)
    snap.add_argument("--out", required=True, type=Path)
    snap.add_argument("--format", choices=("text", "json"), default="text", help="Console output format")
    diff = commands.add_parser("compare", help="Compare two snapshots")
    diff.add_argument("old", type=Path)
    diff.add_argument("new", type=Path)
    diff.add_argument("--format", choices=("text", "json"), default="text")
    diff.add_argument("--out", type=Path, help="Optional report path")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            db = args.database
            # Include sidecars of both the supplied path and its resolved target:
            # a symlink to a database must not allow overwriting its real WAL.
            resolved = db.resolve()
            inputs = [db, resolved, *(Path(str(root) + suffix) for root in (db, resolved) for suffix in ("-wal", "-shm", "-journal"))]
            _check_output(args.out, inputs)
            snapshot = inspect_database(db)
            data = encode_json(snapshot)
            _write_atomic(args.out, data)
            sys.stdout.write(data if args.format == "json" else f'Snapshot written: {len(snapshot["objects"])} objects.\n')
            return 0
        if args.out is not None:
            _check_output(args.out, [args.old, args.new])
        report = compare(read_snapshot(args.old), read_snapshot(args.new))
        output = encode_json(report) if args.format == "json" else _text_diff(report)
        if args.out is not None:
            _write_atomic(args.out, output)
        sys.stdout.write(output)
        return 0 if report["equal"] else 1
    except (LensError, OSError, ValueError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, LensError) else "Could not process the requested inputs."
        print(f"sqlite-schema-lens: {message}", file=sys.stderr)
        return 2
