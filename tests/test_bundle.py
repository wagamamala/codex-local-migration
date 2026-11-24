"""End-to-end tests using entirely synthetic Codex homes."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

SCRIPT = Path(__file__).parents[1] / "skills/codex-local-migration/scripts/native_bundle.py"
SELECTED = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


def encoded(value):
    return (json.dumps(value) + "\n").encode()


def write_rollout(path, ident, turn_ids):
    first = encoded({"type": "session_meta", "payload": {"id": ident, "cwd": "/example/project"}})
    data = bytearray(first)
    offsets = []
    for turn in turn_ids:
        start = len(data)
        data.extend(encoded({"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}))
        data.extend(encoded({"type": "response_item", "payload": {"type": "message", "role": "user",
                                                                 "content": [{"type": "input_text", "text": "Synthetic."}]}}))
        offsets.append((turn, start, len(data)))
    path.write_bytes(data)
    return offsets


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "fake-codex"
        sessions = self.home / "sessions/2026/01/01"
        sessions.mkdir(parents=True)
        self.primary = sessions / f"rollout-new-{SELECTED}.jsonl"
        self.previous = sessions / f"rollout-old-{SELECTED}.jsonl"
        self.unrelated = sessions / f"rollout-{OTHER}.jsonl"
        write_rollout(self.primary, SELECTED, ["replacement"])
        offsets = write_rollout(self.previous, SELECTED, ["old-one", "old-two"])
        write_rollout(self.unrelated, OTHER, ["unrelated"])
        index = [
            {"id": SELECTED, "thread_name": "old label"},
            {"id": OTHER, "thread_name": "unrelated"},
            {"id": SELECTED, "thread_name": "current label"},
        ]
        (self.home / "session_index.jsonl").write_bytes(b"".join(map(encoded, index)))
        (self.home / "auth.json").write_text("synthetic credential file")
        (self.home / ".codex-global-state.json").write_text(json.dumps({
            "local-projects": {"p1": {"name": "Synthetic"}, "p2": {"name": "Unrelated"}},
            "thread-project-assignments": {
                SELECTED: {"projectKind": "local", "projectId": "p1"},
                OTHER: {"projectKind": "local", "projectId": "p2"},
            },
        }))
        with sqlite3.connect(self.home / "state_5.sqlite") as s:
            s.executescript("""
                CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, name TEXT, cwd TEXT, history_mode TEXT, project_id TEXT, updated_at_ms INTEGER);
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE project_roots (project_id TEXT, path TEXT);
            """)
            s.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", [
                (SELECTED, str(self.primary), "Selected", "/example/project", "paginated", None, 1),
                (OTHER, str(self.unrelated), "Unrelated", "/example/other", "paginated", None, 1),
            ])
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as h:
            h.executescript("""
                CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
                                           rollout_byte_offset INTEGER, rollout_end_byte_offset INTEGER, status TEXT);
                CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT);
            """)
            h.executemany("INSERT INTO thread_turns VALUES (?,?,?,?,?,?)", [
                (SELECTED, turn, i, start, end, "completed") for i, (turn, start, end) in enumerate(offsets)
            ])
            h.execute("INSERT INTO thread_items VALUES (?,?,?)", (SELECTED, 1, '{"synthetic":true}'))
            h.execute("INSERT INTO thread_items VALUES (?,?,?)", (OTHER, 1, '{"unrelated":true}'))
        self.archive = self.root / "private.zip"

    def run_cli(self, *args):
        res = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            capture_output=True, text=True,
        )
        return res

    def export(self):
        return self.run_cli("export", "--codex-home", self.home, "--thread-id", SELECTED, "--output", self.archive)

    def test_selected_bytes_and_offsets(self):
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        verified = self.run_cli("verify", self.archive)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        with ZipFile(self.archive) as z:
            manifest = json.loads(z.read("manifest.json"))
            thread, = manifest["threads"]
            self.assertEqual(thread["id"], SELECTED)
            self.assertEqual(thread["desktop_project_id"], "p1")
            by_name = {Path(item["member"]).name: item for item in thread["rollouts"]}
            self.assertTrue(by_name[self.primary.name]["registered"])
            self.assertEqual(by_name[self.primary.name]["projection_check"]["matched_turns"], 0)
            self.assertEqual(by_name[self.previous.name]["projection_check"]["matched_turns"], 2)
            for original in (self.primary, self.previous):
                self.assertEqual(z.read(by_name[original.name]["member"]), original.read_bytes())
            index = z.read("native/session_index.jsonl").splitlines()
            self.assertEqual([json.loads(line)["thread_name"] for line in index], ["old label", "current label"])
            self.assertNotIn(OTHER, "\n".join(z.namelist()))
            self.assertNotIn(b"unrelated", z.read("database/history/thread_items.jsonl"))
            desktop = z.read("desktop-projects.json")
            self.assertNotIn(b"p2", desktop)
            self.assertNotIn("auth.json", z.namelist())

    def test_preserves_candidate_when_registered_rollout_is_missing(self):
        self.primary.unlink()
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(self.archive) as z:
            thread, = json.loads(z.read("manifest.json"))["threads"]
            self.assertTrue(thread["registered_rollout_missing"])
            candidate, = thread["rollouts"]
            self.assertFalse(candidate["registered"])
            self.assertEqual(candidate["projection_check"]["matched_turns"], 2)

    def test_reports_new_thread_scoped_table_for_review(self):
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("CREATE TABLE future_events (thread_id TEXT, event_json TEXT)")
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(self.archive) as z:
            manifest = json.loads(z.read("manifest.json"))
            self.assertIn("future_events", manifest["unhandled_scoped_tables"]["history"])
            self.assertNotIn("database/history/future_events.jsonl", z.namelist())

    @unittest.skipUnless(os.environ.get("CODEX_TEST_ZSTD") or shutil.which("zstd"), "zstd CLI unavailable")
    def test_expands_compressed_registered_rollout_and_preserves_source_bytes(self):
        zstd = os.environ.get("CODEX_TEST_ZSTD") or shutil.which("zstd")
        raw = self.primary.read_bytes()
        packed = self.primary.with_suffix(".jsonl.zst")
        subprocess.run([zstd, "-q", "-o", str(packed), str(self.primary)], check=True)
        compressed = packed.read_bytes()
        self.primary.unlink()
        result = self.run_cli("export", "--codex-home", self.home, "--thread-id", SELECTED,
                              "--zstd", zstd, "--output", self.archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(self.archive) as z:
            thread, = json.loads(z.read("manifest.json"))["threads"]
            candidate, = [log for log in thread["rollouts"] if log["registered"]]
            self.assertFalse(thread["registered_rollout_missing"])
            self.assertEqual(z.read(candidate["member"]), raw)
            self.assertEqual(z.read(candidate["source_member"]), compressed)
        self.assertFalse(self.primary.exists())
        self.assertEqual(packed.read_bytes(), compressed)

    def test_verify_detects_changed_payload(self):
        self.assertEqual(self.export().returncode, 0)
        changed = self.root / "changed.zip"
        with ZipFile(self.archive) as src, ZipFile(changed, "w") as dst:
            for name in src.namelist():
                body = src.read(name)
                if name == "native/session_index.jsonl":
                    body += b"\n"
                dst.writestr(name, body)
        result = self.run_cli("verify", changed)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Integrity mismatch", result.stderr)

    def test_refuses_pattern_and_does_not_publish_partial_result(self):
        with self.primary.open("ab") as f:
            f.write(encoded({"synthetic_marker": "-----BEGIN " + "PRIVATE KEY-----"}))
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credential", result.stderr.lower())
        self.assertFalse(self.archive.exists())

    def test_reads_committed_wal_without_changing_the_source(self):
        db_path = self.home / "state_5.sqlite"
        db = sqlite3.connect(db_path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("UPDATE threads SET name='Recently renamed' WHERE id=?", (SELECTED,))
        db.commit()
        wal_path = db_path.with_suffix(".sqlite-wal")
        before = (db_path.read_bytes(), wal_path.read_bytes())
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((db_path.read_bytes(), wal_path.read_bytes()), before)
        with ZipFile(self.archive) as archive:
            thread, = json.loads(archive.read("manifest.json"))["threads"]
            self.assertEqual(thread["name"], "Recently renamed")

    def test_refuses_active_conversation(self):
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as h:
            h.execute("UPDATE thread_turns SET status='inProgress'")
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-terminal", result.stderr)
        self.assertFalse(self.archive.exists())

    def test_keeps_existing_output_and_rejects_unknown_thread(self):
        self.archive.write_bytes(b"existing")
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.archive.read_bytes(), b"existing")
        result = self.run_cli("export", "--codex-home", self.home, "--thread-id",
                              "00000000-0000-4000-8000-000000000003", "--output", self.root / "missing.zip")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown thread", result.stderr)


if __name__ == "__main__":
    unittest.main()
