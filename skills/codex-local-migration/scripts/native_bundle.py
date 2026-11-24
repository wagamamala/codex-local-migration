#!/usr/bin/env python3
"""Export and verify selected native Codex sessions without modifying a Codex home."""

import argparse
import base64
from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

CHUNK = 1024 * 1024
FORMAT = 1
TERMINAL = {"completed", "interrupted", "failed", "cancelled", "canceled"}
CREDENTIALS = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
    rb"|(?<![\w-])(?:sk-(?:(?:proj|svcacct|ant)-)?[\w-]{24,}|gh[pousr]_[\w]{30,}|"
    rb"github_pat_[\w-]{30,}|xox[bpars]-[\w-]{20,}|ya29\.[\w.-]{25,}|AKIA[A-Z0-9]{16})"
    rb"|(?i:bearer[ \t]+eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
STATE_SCOPES = {
    "thread_attachments": "thread_id",
    "thread_dynamic_tools": "thread_id",
    "thread_read_receipts": "thread_id",
    "projects": "id",
    "project_roots": "project_id",
}
HISTORY_TABLES = (
    "thread_turns", "thread_items", "thread_realtime_items", "thread_history_projection_state",
)


class BundleError(Exception):
    pass


def uuid_text(value):
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("Expected a thread UUID") from exc


def blob(value):
    if isinstance(value, bytes):
        return {"$type": "sqlite_blob", "base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Unsupported SQLite type {type(value)!r}")


def json_line(value):
    return (json.dumps(value, ensure_ascii=True, default=blob, separators=(",", ":")) + "\n").encode()


def open_snapshot(path):
    db = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    db.execute("BEGIN")
    db.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
    return db


def placeholders(values):
    return ",".join("?" for _ in values)


def filtered(db, table, key, values):
    if not values:
        return []
    return [dict(row) for row in db.execute(
        f'SELECT * FROM "{table}" WHERE "{key}" IN ({placeholders(values)})', tuple(values)
    )]


def database_path(explicit, root, prefix, optional=False):
    if explicit:
        return explicit.expanduser().resolve(strict=True)
    found = [p for p in root.glob(f"{prefix}_*.sqlite") if p.is_file()]
    if not found and optional:
        return None
    if len(found) != 1:
        flag = "--state-db" if prefix == "state" else "--history-db"
        raise BundleError(f"Expected one {prefix} database, found {len(found)}. Specify {flag}.")
    return found[0]


def read_desktop(home):
    file = home / ".codex-global-state.json"
    if not file.is_file():
        return {}
    return json.loads(file.read_bytes())


def archive_name(home, path):
    path = path.resolve(strict=True)
    roots = [root.resolve() for root in (home / "sessions", home / "archived_sessions")]
    if not path.name.endswith((".jsonl", ".jsonl.zst")) or not path.is_relative_to(home) or not any(
        path.is_relative_to(root) for root in roots
    ):
        raise BundleError(f"Rollout falls outside the selected Codex session directories: {path.name}")
    return "native/" + path.relative_to(home).as_posix()


def session_meta(path):
    with path.open("rb") as src:
        first = json.loads(src.readline())
    if first.get("type") != "session_meta":
        raise BundleError(f"Missing first-line session_meta: {path.name}")
    payload = first.get("payload", {})
    return payload.get("id") or payload.get("session_id"), payload.get("cwd")


def file_stamp(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def offset_report(path, turns):
    """Check whether indexed turn starts point to their own JSONL event."""
    result = {"turns": len(turns), "matched_turns": 0, "unavailable_offsets": 0, "mismatches": 0}
    size = path.stat().st_size
    with path.open("rb") as file:
        for turn in turns:
            start = turn.get("rollout_byte_offset")
            end = turn.get("rollout_end_byte_offset")
            if start is None:
                result["unavailable_offsets"] += 1
                continue
            if not 0 <= start < size or (end is not None and not start <= end <= size):
                result["mismatches"] += 1
                continue
            if start:
                file.seek(start - 1)
                if file.read(1) != b"\n":
                    result["mismatches"] += 1
                    continue
            file.seek(start)
            try:
                marker = json.loads(file.readline())
                payload = marker.get("payload", {})
                matches = bool(turn.get("turn_id")) and payload.get("turn_id") == turn["turn_id"]
            except (ValueError, AttributeError):
                matches = False
            result["matched_turns" if matches else "mismatches"] += 1
    return result


def decode_rollout(path, directory, zstd):
    executable = zstd or shutil.which("zstd")
    if not executable:
        raise BundleError(f"{path.name} is Zstandard-compressed. Pass --zstd with a trusted zstd executable.")
    target = directory / f"rollout-{uuid.uuid4().hex}.jsonl"
    with target.open("xb") as out:
        result = subprocess.run([executable, "-q", "-d", "-c", "--", str(path)],
                                stdout=out, stderr=subprocess.PIPE)
    if result.returncode:
        raise BundleError(f"Could not decompress {path.name} (zstd exit {result.returncode})")
    return target


def collect_rollouts(home, threads, turns, staging, zstd):
    candidates = {row["id"]: set() for row in threads}
    content_paths = {}
    stamps = {}

    def prepare(source):
        archive_name(home, source)
        source = source.resolve(strict=True)
        if source not in content_paths:
            stamps[source] = file_stamp(source)
            content_paths[source] = decode_rollout(source, staging, zstd) if source.suffix == ".zst" else source
        return source

    for directory in (home / "sessions", home / "archived_sessions"):
        for path in directory.rglob("*.jsonl*"):
            if not path.name.endswith((".jsonl", ".jsonl.zst")):
                continue
            wanted = [ident for ident in candidates if ident in path.name]
            if wanted:
                source = prepare(path)
                # Fork filenames can contain more than one UUID. Trust session metadata.
                ident, _ = session_meta(content_paths[source])
                if ident in wanted:
                    candidates[ident].add(source)
    selected = []
    for row in threads:
        ident = row["id"]
        registered_path = row.get("rollout_path")
        active = Path(registered_path).expanduser() if registered_path else None
        if active and not active.is_absolute():
            active = home / active
        if active and not active.is_file() and active.name.endswith(".jsonl"):
            compressed = active.with_name(active.name + ".zst")
            if compressed.is_file():
                active = compressed
        active = prepare(active) if active and active.is_file() else None
        available = candidates[ident] | ({active} if active else set())
        if not available:
            raise BundleError(f"No native rollout found for thread {ident}")
        rollouts = []
        matching = [turn for turn in turns if turn["thread_id"] == ident]
        for path in sorted(available):
            original = archive_name(home, path)
            log = content_paths[path]
            log_id, old_cwd = session_meta(log)
            if log_id != ident:
                raise BundleError(f"Rollout identity mismatch for {path.name}")
            member = original if path == log else "decoded/" + path.relative_to(home).as_posix().removesuffix(".zst")
            rollouts.append({"member": member, "source_member": original, "source": str(path),
                             "source_compression": "zstd" if path != log else None,
                             "registered": path == active, "recorded_cwd": old_cwd,
                             "projection_check": offset_report(log, matching)})
        selected.append({key: row.get(key) for key in (
            "id", "name", "title", "cwd", "archived", "history_mode", "project_id",
            "attached_thread_parent_id", "updated_at_ms",
        )} | {"rollouts": rollouts, "registered_rollout_missing": active is None})
    return selected, stamps, content_paths

def table_names(db):
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


def unhandled_scoped_tables(db, exported):
    if not db:
        return []
    scoped_columns = {"thread_id", "parent_thread_id", "child_thread_id", "project_id"}
    return sorted(table for table in table_names(db) - set(exported)
                  if scoped_columns.intersection(row["name"] for row in
                      db.execute("SELECT name FROM pragma_table_info(?)", (table,))))


def export_tables(writer, db, label, tables, source):
    names = sorted(tables)
    schema = db.execute(
        f"SELECT sql FROM sqlite_schema WHERE tbl_name IN ({placeholders(names)}) AND sql IS NOT NULL "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name", names,
    )
    writer.write(f"database/{label}/schema.sql", [(row["sql"] + ";\n").encode() for row in schema])
    for name, rows in tables.items():
        writer.write(f"database/{label}/{name}.jsonl", map(json_line, rows))
    writer.write(f"database/{label}/metadata.json", [json_line({
        "source_file": source.name,
        "sqlite_version": sqlite3.sqlite_version,
        "user_version": db.execute("PRAGMA user_version").fetchone()[0],
        "schema_version": db.execute("PRAGMA schema_version").fetchone()[0],
        "row_counts": {name: len(rows) for name, rows in tables.items()},
    })])


class ArchiveWriter:
    def __init__(self, archive):
        self.archive = archive
        self.entries = {}

    def write(self, member, chunks, check_credentials=True):
        digest = hashlib.sha256()
        size = 0
        with self.archive.open(member, "w", force_zip64=True) as out:
            for chunk in chunks:
                if check_credentials and CREDENTIALS.search(chunk):
                    raise BundleError(f"Possible credential in {member}. Review locally before exporting.")
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        self.entries[member] = {"sha256": digest.hexdigest(), "size": size}


def check_files(archive):
    names = archive.namelist()
    if len(set(names)) != len(names):
        raise BundleError("Duplicate ZIP member")
    for entry in archive.infolist():
        name = PurePosixPath(entry.filename)
        mode = entry.external_attr >> 16
        if name.is_absolute() or ".." in name.parts or "\\" in entry.filename or stat.S_ISLNK(mode):
            raise BundleError(f"Unsafe ZIP member: {entry.filename}")
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("format") != FORMAT:
        raise BundleError("Unsupported bundle format")
    entries = manifest["files"]
    if set(entries) != set(names) - {"manifest.json"}:
        raise BundleError("Integrity mismatch: ZIP member set differs from manifest")
    for member, expected in entries.items():
        digest = hashlib.sha256()
        size = 0
        with archive.open(member) as file:
            for chunk in iter(lambda: file.read(CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
        if {"sha256": digest.hexdigest(), "size": size} != expected:
            raise BundleError(f"Integrity mismatch: {member}")
    return manifest


def codex_version():
    try:
        result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def scoped_rows(state, history, home, threads, ids):
    desktop = read_desktop(home)
    assignments = desktop.get("thread-project-assignments", {})
    host_map = desktop.get("thread-project-membership-host-ids", {})
    state_names = table_names(state)
    history_names = table_names(history) if history else set()
    projects = {row["project_id"] for row in threads if row.get("project_id")}
    for ident in ids:
        mapped = assignments.get(ident) or {}
        if mapped.get("projectKind") == "local" and mapped.get("projectId"):
            projects.add(mapped["projectId"])
    desktop_metadata = {
        "local_projects": {key: item for key, item in desktop.get("local-projects", {}).items() if key in projects},
        "thread_project_assignments": {key: assignments.get(key) for key in ids},
        "thread_project_membership_host_ids": {key: host_map.get(key) for key in ids},
    }
    project_tables = {"projects", "project_roots"}
    state_rows = {"threads": threads}
    for table, key in STATE_SCOPES.items():
        if table in state_names:
            state_rows[table] = filtered(state, table, key, projects if table in project_tables else ids)
    if "thread_spawn_edges" in state_names:
        state_rows["thread_spawn_edges"] = [dict(row) for row in state.execute(
            f"SELECT * FROM thread_spawn_edges WHERE parent_thread_id IN ({placeholders(ids)}) "
            f"OR child_thread_id IN ({placeholders(ids)})", (*ids, *ids)
        )]
    history_rows = {table: filtered(history, table, "thread_id", ids)
                    for table in HISTORY_TABLES if table in history_names}
    if "_sqlx_migrations" in state_names:
        state_rows["_sqlx_migrations"] = [dict(row) for row in state.execute("SELECT * FROM _sqlx_migrations")]
    if "_sqlx_migrations" in history_names:
        history_rows["_sqlx_migrations"] = [dict(row) for row in history.execute("SELECT * FROM _sqlx_migrations")]
    return state_rows, history_rows, desktop_metadata


def export(args):
    home = args.codex_home.expanduser().resolve(strict=True)
    db_home = args.sqlite_home.expanduser().resolve(strict=True) if args.sqlite_home else home
    output = args.output.expanduser().resolve()
    if output.is_relative_to(home) or output.is_relative_to(db_home) or output.exists():
        raise BundleError("Choose a new output ZIP outside the active Codex and database directories.")
    ids = tuple(dict.fromkeys(args.thread_id))
    state_file = database_path(args.state_db, db_home, "state")
    history_file = database_path(args.history_db, db_home, "thread_history", optional=True)
    with ExitStack() as stack:
        state = stack.enter_context(closing(open_snapshot(state_file)))
        history = stack.enter_context(closing(open_snapshot(history_file))) if history_file else None
        rows = filtered(state, "threads", "id", ids)
        by_id = {row["id"]: row for row in rows}
        if by_id.keys() != set(ids):
            raise BundleError("Unknown thread: " + ", ".join(sorted(set(ids) - by_id.keys())))
        state_rows, history_rows, desktop = scoped_rows(state, history, home, rows, ids)
        unhandled = {"state": unhandled_scoped_tables(state, state_rows),
                     "history": unhandled_scoped_tables(history, history_rows)}
        turns = history_rows.get("thread_turns", [])
        for turn in turns:
            status = turn.get("status")
            if status and str(status).lower() not in TERMINAL:
                raise BundleError(f"Thread {turn['thread_id']} has a non-terminal or unrecognized turn status. End active work and retry.")
        staging = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".codex-expand-", dir=output.parent)))
        selected, stamps, contents = collect_rollouts(home, rows, turns, staging, args.zstd)
        for thread in selected:
            edge = desktop["thread_project_assignments"].get(thread["id"]) or {}
            thread["desktop_project_id"] = edge.get("projectId")
        index = home / "session_index.jsonl"
        index_lines = []
        if index.is_file():
            with index.open("rb") as src:
                index_lines = [line for line in src if line.strip() and json.loads(line).get("id") in by_id]
        index_counts = {ident: 0 for ident in ids}
        for line in index_lines:
            index_counts[json.loads(line)["id"]] += 1
        with tempfile.TemporaryDirectory(prefix=".codex-native-", dir=output.parent) as temp:
            draft = Path(temp) / "bundle.zip"
            with draft.open("xb") as handle:
                draft.chmod(0o600)
                with ZipFile(handle, "w", compression=ZIP_DEFLATED, compresslevel=2) as archive:
                    writer = ArchiveWriter(archive)
                    for thread in selected:
                        for item in thread["rollouts"]:
                            original = Path(item["source"])
                            with contents[original].open("rb") as src:
                                writer.write(item["member"], src)
                            if item["source_member"] != item["member"]:
                                with original.open("rb") as src:
                                    writer.write(item["source_member"], iter(lambda: src.read(CHUNK), b""),
                                                 check_credentials=False)
                            if writer.entries[item["source_member"]]["size"] != stamps[original][2]:
                                raise BundleError("Rollout changed while being copied. Retry after all writes finish.")
                    if index.is_file():
                        writer.write("native/session_index.jsonl", index_lines)
                    writer.write("desktop-projects.json", [json_line(desktop)])
                    export_tables(writer, state, "state", state_rows, state_file)
                    if history:
                        export_tables(writer, history, "history", history_rows, history_file)
                    archive.writestr("manifest.json", json_line({
                        "format": FORMAT, "codex_version": codex_version(), "threads": selected,
                        "index_rows_per_thread": index_counts, "unhandled_scoped_tables": unhandled,
                        "files": writer.entries,
                        "table_counts": {
                            "state": {k: len(v) for k, v in state_rows.items()},
                            "history": {k: len(v) for k, v in history_rows.items()},
                        },
                    }))
            with ZipFile(draft) as zipped:
                manifest = check_files(zipped)
            with closing(open_snapshot(state_file)) as fresh:
                if {row["id"]: row for row in filtered(fresh, "threads", "id", ids)} != by_id:
                    raise BundleError("Thread metadata changed during export. Retry after all writes finish.")
            if history_file:
                with closing(open_snapshot(history_file)) as fresh:
                    for table in ("thread_turns", "thread_history_projection_state"):
                        if table in history_rows:
                            current = filtered(fresh, table, "thread_id", ids)
                            if sorted(map(json_line, current)) != sorted(map(json_line, history_rows[table])):
                                raise BundleError("History changed during export. Retry after all writes finish.")
            if any(file_stamp(path) != before for path, before in stamps.items()):
                raise BundleError("A rollout changed during export. Retry after all writes finish.")
            os.link(draft, output)
    print(json.dumps({"output": str(output), "threads": len(manifest["threads"]),
                      "files": len(manifest["files"]), "unhandled_scoped_tables": unhandled,
                      "threads_with_missing_registered_rollout": [
                          entry["id"] for entry in selected if entry["registered_rollout_missing"]
                      ]}, ensure_ascii=False))


def verify(args):
    with ZipFile(args.bundle) as archive:
        manifest = check_files(archive)
    print(json.dumps({"verified": True, "threads": manifest["threads"],
                      "index_rows_per_thread": manifest["index_rows_per_thread"],
                      "table_counts": manifest["table_counts"],
                      "unhandled_scoped_tables": manifest["unhandled_scoped_tables"]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("export", help="Read selected sessions and write a private ZIP")
    create.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    create.add_argument("--sqlite-home", type=Path, default=os.environ.get("CODEX_SQLITE_HOME"))
    create.add_argument("--state-db", type=Path, help="Explicit state database if several versions exist")
    create.add_argument("--history-db", type=Path, help="Explicit history database if several versions exist")
    create.add_argument("--thread-id", type=uuid_text, action="append", required=True)
    create.add_argument("--zstd", help="Trusted zstd executable for older .jsonl.zst rollouts")
    create.add_argument("--output", type=Path, required=True)
    create.set_defaults(handler=export)
    read = commands.add_parser("verify", help="Verify every bundle member and report candidate history matches")
    read.add_argument("bundle", type=Path)
    read.set_defaults(handler=verify)
    args = parser.parse_args()
    try:
        args.handler(args)
    except (BundleError, OSError, sqlite3.Error, BadZipFile, KeyError, ValueError) as err:
        parser.exit(1, f"Error: {err}\n")


if __name__ == "__main__":
    main()
