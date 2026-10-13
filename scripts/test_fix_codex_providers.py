import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import fix_codex_providers as repair


class ProviderRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "config.toml").write_text(
            'model_provider = "custom"\n[model_providers.custom]\nname = "custom"\n', encoding="utf-8")
        self.path = self.home / "sessions" / "rollout-test.jsonl"
        self.path.parent.mkdir()
        self.backup = self.home / "backups"
        self.backup.mkdir()
        self.fix = lambda provider: provider == "wuzu_api"

    def test_only_direct_metadata_provider_changes(self):
        line = (b'\xef\xbb\xbf{ "payload": {"note": "model_provider wuzu_api", '
                b'"nested": {"model_provider":"wuzu_api"}, "model_provider" : "wuzu_api"}, '
                b'"type":"session_meta" }\r\n')
        changed, provider, did_change = repair.transform_line(line, self.fix, "custom")
        self.assertTrue(did_change)
        self.assertEqual(provider, "wuzu_api")
        self.assertEqual(changed, line.replace(b'"model_provider" : "wuzu_api"',
                                               b'"model_provider" : "custom"'))

    def test_non_metadata_and_escaped_keys(self):
        line = b'{"type":"response_item","payload":{"model_provider":"wuzu_api","text":"session_meta"}}\n'
        self.assertEqual(repair.transform_line(line, self.fix, "custom")[0], line)
        escaped = b'{"type":"session_\\u006deta","payload":{"model_\\u0070rovider":"wuzu_api"}}\n'
        updated = repair.transform_line(escaped, self.fix, "custom")[0]
        self.assertEqual(json.loads(updated)["payload"]["model_provider"], "custom")
        self.assertIn(b'model_\\u0070rovider', updated)

    def test_late_metadata_backup_and_idempotence(self):
        prefix = b'{"type":"response_item","payload":"history unchanged"}\r\n' * 2221
        metadata = b'{"type":"session_meta","payload":{"model_provider":"wuzu_api"}}\r\n'
        original = prefix + metadata + b'{"type":"session_meta","payload":{"model_provider":"openai"}}'
        self.path.write_bytes(original)
        item = repair.scan_session(self.path, self.fix, "custom")
        self.assertEqual(item["changes"], [{"line": 2222, "old_provider": "wuzu_api"}])
        repair.apply_session(item, self.home, self.backup, self.fix, "custom", lambda: None)
        self.assertEqual(Path(item["backup"]).read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), original.replace(b'"wuzu_api"', b'"custom"'))
        self.assertEqual(repair.scan_session(self.path, self.fix, "custom")["changes"], [])

    def test_concurrent_append_refuses_to_overwrite(self):
        self.path.write_bytes(b'{"type":"session_meta","payload":{"model_provider":"wuzu_api"}}\n')
        item = repair.scan_session(self.path, self.fix, "custom")
        with self.path.open("ab") as out:
            out.write(b'{"type":"new_message"}\n')
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            repair.apply_session(item, self.home, self.backup, self.fix, "custom", lambda: None)
        self.assertEqual(self.path.read_bytes(), before)

    def test_open_writer_blocks_replacement(self):
        self.path.write_bytes(b'{"type":"session_meta","payload":{"model_provider":"wuzu_api"}}\n')
        item = repair.scan_session(self.path, self.fix, "custom")
        with self.path.open("ab"):
            with self.assertRaises(OSError):
                repair.apply_session(item, self.home, self.backup, self.fix, "custom", lambda: None)

    def test_wal_backup_and_conditional_database_update(self):
        db = self.home / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, title TEXT)")
            writer.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)",
                               [("old", "a", "wuzu_api", "keep title"), ("native", "b", "openai", "keep too")])
            writer.commit()
            item, _ = repair.database_audit(db, self.fix)
            repair.apply_database(item, self.home, self.backup, "custom", lambda: None)
            self.assertEqual(writer.execute("SELECT model_provider,title FROM threads WHERE id='old'").fetchone(),
                             ("custom", "keep title"))
            self.assertEqual(writer.execute("SELECT model_provider FROM threads WHERE id='native'").fetchone()[0], "openai")
            with contextlib.closing(sqlite3.connect(item["backup"])) as saved:
                self.assertEqual(saved.execute("SELECT model_provider FROM threads WHERE id='old'").fetchone()[0], "wuzu_api")
                self.assertEqual(saved.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_database_transaction_rolls_back_on_changed_row(self):
        db = self.home / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(db)) as writer:
            writer.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT)")
            writer.executemany("INSERT INTO threads VALUES (?, ?, ?)",
                               [("first", "a", "wuzu_api"), ("second", "b", "wuzu_api")])
            writer.commit()
            item, _ = repair.database_audit(db, self.fix)
            writer.execute("UPDATE threads SET model_provider='openai' WHERE id='second'")
            writer.commit()
            with self.assertRaises(RuntimeError):
                repair.apply_database(item, self.home, self.backup, "custom", lambda: None)
            self.assertEqual(writer.execute("SELECT model_provider FROM threads WHERE id='first'").fetchone()[0],
                             "wuzu_api")


class SkillWorkflowTests(unittest.TestCase):
    FIRST = "11111111-1111-4111-8111-111111111111"
    SECOND = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "config.toml").write_text(
            'model_provider = "custom"\n[model_providers.custom]\nname = "custom"\n', encoding="utf-8")
        self.sessions = []
        for folder, thread_id in [("sessions", self.FIRST), ("archived_sessions", self.SECOND)]:
            path = self.home / folder / f"rollout-{thread_id}.jsonl"
            path.parent.mkdir()
            path.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": thread_id, "model_provider": "wuzu_api"}}) + "\n", encoding="utf-8")
            self.sessions.append(path)
        self.databases = []
        for name in ("state_5.sqlite", "sqlite/state_5.sqlite"):
            path = self.home / name
            path.parent.mkdir(exist_ok=True)
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, title TEXT)")
                db.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)",
                               [(self.FIRST, str(self.sessions[0]), "wuzu_api", "first title"),
                                (self.SECOND, str(self.sessions[1]), "wuzu_api", "second title")])
                db.commit()
            self.databases.append(path)

    def run_cli(self, *args):
        report = self.home / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = repair.main(["--home", str(self.home), "--everything", str(self.home / "absent.exe"),
                                "--report", str(report), *args])
        return code, json.loads(report.read_text(encoding="utf-8"))

    def test_audit_finds_missing_and_mismatched_without_mutating(self):
        with contextlib.closing(sqlite3.connect(self.databases[0])) as db:
            db.execute("UPDATE threads SET model_provider='custom' WHERE id=?", (self.FIRST,))
            db.commit()
        sources = [self.home / "config.toml", *self.sessions, *self.databases]
        before = {p: p.read_bytes() for p in sources}
        code, report = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(report["summary"]["missing_providers"], ["wuzu_api"])
        self.assertEqual(report["summary"]["issue_counts"]["db_session_provider_mismatch"], 1)
        self.assertEqual(report["summary"]["issue_counts"]["database_provider_mismatch"], 1)
        self.assertEqual(before, {p: p.read_bytes() for p in sources})
        self.assertFalse((self.home / "provider-repair-backups").exists())

    def test_thread_scoped_repair_leaves_other_thread_untouched(self):
        untouched = self.sessions[1].read_bytes()
        code, report = self.run_cli("--thread-id", f"codex://threads/{self.FIRST}", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["summary"]["affected_files"], 1)
        self.assertEqual(self.sessions[1].read_bytes(), untouched)
        self.assertEqual(json.loads(self.sessions[0].read_text())["payload"]["model_provider"], "custom")
        for path in self.databases:
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT model_provider,title FROM threads WHERE id=?", (self.FIRST,)).fetchone(),
                                 ("custom", "first title"))
                self.assertEqual(db.execute("SELECT model_provider FROM threads WHERE id=?", (self.SECOND,)).fetchone()[0],
                                 "wuzu_api")
        _, after = self.run_cli("--thread-id", self.FIRST)
        self.assertEqual(after["summary"]["affected_files"], 0)
        self.assertEqual(sum(after["summary"]["database_rows"].values()), 0)
        self.assertEqual(after["issues"], [])

    def test_configured_source_requires_explicit_mapping(self):
        with (self.home / "config.toml").open("a", encoding="utf-8") as cfg:
            cfg.write('\n[model_providers.wuzu_api]\nname = "still valid"\n')
        _, report = self.run_cli("--apply")
        self.assertEqual(report["summary"]["affected_files"], 0)
        self.assertFalse((self.home / "provider-repair-backups").exists())
        _, report = self.run_cli("--from-provider", "wuzu_api", "--to-provider", "custom", "--apply")
        self.assertEqual(report["summary"]["affected_files"], 2)
        _, after = self.run_cli()
        self.assertEqual(after["issues"], [])

    def test_unconfigured_target_can_be_audited_but_not_applied(self):
        code, report = self.run_cli("--to-provider", "missing-target")
        self.assertEqual(code, 0)
        self.assertFalse(report["target_configured"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli("--to-provider", "missing-target", "--apply")
        self.assertFalse((self.home / "provider-repair-backups").exists())

    def test_archived_path_diagnostic_finds_file_by_thread_id(self):
        old_path = str(self.home / "sessions" / self.sessions[1].name)
        with contextlib.closing(sqlite3.connect(self.databases[1])) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (old_path, self.SECOND))
            db.commit()
        _, report = self.run_cli("--thread-id", self.SECOND)
        issues = [i for i in report["issues"] if i["kind"] == "rollout_path_not_scanned"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["found_by_id"], [str(self.sessions[1])])
        with contextlib.closing(sqlite3.connect(self.databases[1])) as db:
            self.assertEqual(db.execute("SELECT rollout_path FROM threads WHERE id=?", (self.SECOND,)).fetchone()[0], old_path)

    def test_invalid_config_reports_without_changes(self):
        (self.home / "config.toml").write_text('model_provider = [invalid', encoding="utf-8")
        before = [p.read_bytes() for p in self.sessions]
        code, report = self.run_cli("--apply")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "config_error")
        self.assertEqual(before, [p.read_bytes() for p in self.sessions])


if __name__ == "__main__":
    unittest.main()
