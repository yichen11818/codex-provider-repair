#!/usr/bin/env python3
"""Audit/fix stale Codex providers. Python 3.11+, standard library only.

Dry run: python fix_codex_providers.py
Apply:   python fix_codex_providers.py --apply
Only change session_meta.payload.model_provider and threads.model_provider.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timezone

BUILTINS = {"openai", "ollama", "lmstudio"}
EVERYTHING = (shutil.which("es.exe") or shutil.which("es")
              or r"C:\图吧工具箱\图吧工具箱202507\tools\其他工具\Everything\es.exe")
DECODER = json.JSONDecoder()


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def db_connect(path, write=False):
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def database_paths(home):
    return sorted(set(p.resolve() for root in (home, home / "sqlite")
                      if root.is_dir() for p in root.glob("state_*.sqlite")))


def database_audit(path, should_fix, thread_ids=None):
    with contextlib.closing(db_connect(path)) as connection:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(threads)")}
        if not {"id", "model_provider", "rollout_path"} <= columns:
            raise RuntimeError(f"Unsupported threads schema: {path}")
        query = "SELECT id, model_provider, rollout_path FROM threads"
        params = sorted(thread_ids or [])
        if params:
            query += " WHERE id IN (" + ",".join("?" for _ in params) + ")"
        rows = list(connection.execute(query, params))
    return {
        "path": str(path),
        "providers": dict(collections.Counter(r["model_provider"] for r in rows)),
        "threads": [dict(r) for r in rows],
        "changes": [{"id": r["id"], "old_provider": r["model_provider"]}
                    for r in rows if should_fix(r["model_provider"])],
    }, [r["rollout_path"] for r in rows if r["rollout_path"]]


def rollout_path(raw):
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return Path(raw).resolve()


def discover_sessions(home, es, db_rollouts):
    paths = set()
    for name in ("sessions", "archived_sessions"):
        root = home / name
        if not root.is_dir():
            continue
        indexed = []
        if es.is_file():
            with tempfile.TemporaryDirectory(prefix="codex-provider-index-") as tmp:
                export = Path(tmp) / "paths.txt"
                try:
                    result = subprocess.run([str(es), "-export-txt", str(export),
                                             str(root) + os.sep, "rollout-*.jsonl"],
                                            capture_output=True, timeout=30)
                    if result.returncode == 0 and export.exists():
                        indexed = [Path(s).resolve() for s in
                                   export.read_text(encoding="utf-8-sig").splitlines() if s]
                except (OSError, UnicodeError, subprocess.TimeoutExpired):
                    pass  # Use the scoped fallback below; no machine-wide walk.
        if not indexed:
            emit({"notice": "Everything unavailable or scope not indexed; scoped fallback",
                  "directory": str(root)})
            indexed = list(root.rglob("rollout-*.jsonl"))
        paths.update(p for p in indexed if p.is_relative_to(root) and p.is_file())
    for raw in db_rollouts:
        p = rollout_path(raw)
        if (p.name.startswith("rollout-") and p.suffix == ".jsonl" and p.is_file()
                and any(p.is_relative_to(home / n) for n in ("sessions", "archived_sessions"))):
            paths.add(p)
    return sorted(paths)


def member_span(text, start, key):
    """Locate a JSON object's direct member value; never replace text in messages."""
    i = start
    while text[i].isspace():
        i += 1
    if text[i] != "{":
        raise ValueError("Expected JSON object")
    i += 1
    while True:
        while text[i].isspace():
            i += 1
        if text[i] == "}":
            raise KeyError(key)
        name, i = DECODER.raw_decode(text, i)
        while text[i].isspace():
            i += 1
        if text[i] != ":":
            raise ValueError("Expected colon")
        i += 1
        while text[i].isspace():
            i += 1
        begin = i
        _, end = DECODER.raw_decode(text, i)
        if name == key:
            return begin, end
        i = end
        while text[i].isspace():
            i += 1
        if text[i] == "}":
            raise KeyError(key)
        if text[i] != ",":
            raise ValueError("Expected comma")
        i += 1


def transform_line(line, should_fix, target, metadata=None):
    # Also parse escaped-key JSON if present, without parsing every chat message.
    if b"session_meta" not in line and b"\\u" not in line:
        return line, None, False
    try:
        text = line.decode("utf-8-sig")
        data = json.loads(text)
    except (ValueError, UnicodeError):
        if b"session_meta" in line:
            raise ValueError("Malformed JSON line containing session_meta") from None
        return line, None, False
    if not isinstance(data, dict) or data.get("type") != "session_meta":
        return line, None, False
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("session_meta payload is not an object")
    provider = payload.get("model_provider")
    if provider is not None and not isinstance(provider, str):
        raise ValueError("model_provider is not a string")
    if metadata is not None:
        thread_id = payload.get("id") or payload.get("session_id")
        metadata.append({"id": thread_id if isinstance(thread_id, str) else None,
                         "provider": provider})
    if not should_fix(provider):
        return line, provider, False
    begin, _ = member_span(text, 0, "payload")
    begin, end = member_span(text, begin, "model_provider")
    updated = text[:begin] + json.dumps(target, ensure_ascii=False) + text[end:]
    check = json.loads(updated)
    check["payload"]["model_provider"] = provider
    if check != data:
        raise ValueError("Provider edit changed other data")
    bom = b"\xef\xbb\xbf" if line.startswith(b"\xef\xbb\xbf") else b""
    return bom + updated.encode("utf-8"), provider, True


def scan_session(path, should_fix, target):
    before = path.stat()
    counts = collections.Counter()
    changes = []
    metadata = []
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for number, line in enumerate(source, 1):
            digest.update(line)
            previous = len(metadata)
            _, provider, changed = transform_line(line, should_fix, target, metadata)
            if len(metadata) != previous:
                metadata[-1]["line"] = number
            if provider is not None:
                counts[provider] += 1
            if changed:
                changes.append({"line": number, "old_provider": provider})
    after = path.stat()
    if changes and (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Session changed during audit; rerun after its turn finishes")
    return {"path": str(path), "bytes": after.st_size, "mtime_ns": after.st_mtime_ns,
            "sha256": digest.hexdigest(), "providers": dict(counts), "changes": changes,
            "metadata": metadata}


def consistency_issues(databases, sessions):
    """Report discrepancies without deciding which configured provider is intended."""
    issues = []
    by_path = {rollout_path(s["path"]): s for s in sessions}
    by_id = collections.defaultdict(list)
    for session in sessions:
        for thread_id in {m["id"] for m in session["metadata"] if m["id"]}:
            by_id[thread_id].append(session)
        providers = sorted(session["providers"])
        if len(providers) > 1:
            issues.append({"kind": "multiple_metadata_providers", "path": session["path"],
                           "providers": providers})
        if not session["metadata"]:
            issues.append({"kind": "missing_session_meta", "path": session["path"]})
    db_providers = collections.defaultdict(list)
    for database in databases:
        for row in database["threads"]:
            db_providers[row["id"]].append({"database": database["path"],
                                              "provider": row["model_provider"]})
            raw = row["rollout_path"]
            session = by_path.get(rollout_path(raw)) if raw else None
            if session is None:
                matches = by_id.get(row["id"], [])
                if len(matches) == 1:
                    session = matches[0]
                issues.append({"kind": "rollout_path_not_scanned", "database": database["path"],
                               "id": row["id"], "rollout_path": raw,
                               "found_by_id": [s["path"] for s in matches]})
            if session is not None:
                metadata = session["metadata"]
                latest = metadata[-1]["provider"] if metadata else None
                if latest and latest != row["model_provider"]:
                    issues.append({"kind": "db_session_provider_mismatch", "database": database["path"],
                                   "id": row["id"], "path": session["path"],
                                   "database_provider": row["model_provider"],
                                   "last_metadata_provider": latest})
    for thread_id, entries in db_providers.items():
        if len({e["provider"] for e in entries}) > 1:
            issues.append({"kind": "database_provider_mismatch", "id": thread_id,
                           "entries": entries})
    return issues


def parse_thread_id(value):
    prefix = "codex://threads/"
    if value.startswith(prefix):
        value = value[len(prefix):].split("?", 1)[0].rstrip("/")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a thread UUID or codex://threads/UUID") from None


@contextlib.contextmanager
def stable_read(path):
    """On Windows, deny write handles while allowing atomic replacement."""
    if os.name != "nt":
        raise RuntimeError("Apply currently requires Windows for enforced writer exclusion")
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as source:
        yield source


def replace_locked_file(temporary, destination):
    # MoveFileEx (used by os.replace) cannot replace a write-denied target.
    # ReplaceFileW can, while the stable_read handle continues to exclude writers.
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    replace = kernel.ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    replace.restype = wintypes.BOOL
    if not replace(str(destination), str(temporary), None, 2, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def apply_session(item, home, backup, should_fix, target, journal):
    path = Path(item["path"])
    destination = backup / "files" / path.relative_to(home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with stable_read(path) as source:
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != (item["bytes"], item["mtime_ns"]):
                raise RuntimeError("Session changed since audit; rerun")
            fd, name = tempfile.mkstemp(prefix=path.name + ".provider-", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            original_hash, new_hash = hashlib.sha256(), hashlib.sha256()
            changes = []
            with os.fdopen(fd, "wb") as out, destination.open("xb") as saved:
                for number, line in enumerate(source, 1):
                    original_hash.update(line)
                    saved.write(line)
                    replacement, provider, changed = transform_line(line, should_fix, target)
                    if changed:
                        changes.append({"line": number, "old_provider": provider})
                    new_hash.update(replacement)
                    out.write(replacement)
                for stream in (saved, out):
                    stream.flush()
                    os.fsync(stream.fileno())
            if original_hash.hexdigest() != item["sha256"] or changes != item["changes"]:
                raise RuntimeError("Session content changed since audit; no replacement performed")
            shutil.copystat(path, destination)
            shutil.copystat(path, temporary)
            item.update(backup=str(destination), new_sha256=new_hash.hexdigest(), status="prepared")
            journal()
            replace_locked_file(temporary, path)
            temporary = None
        item["status"] = "applied"
        journal()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_database(item, home, backup, target, journal):
    path = Path(item["path"])
    destination = backup / "databases" / path.relative_to(home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(db_connect(path, write=True)) as connection:
        # The SQLite online backup API includes committed WAL data.
        started = time.monotonic()
        def progress(status, remaining, total):
            if time.monotonic() - started > 45:
                raise TimeoutError("SQLite backup timed out")
        with contextlib.closing(sqlite3.connect(destination)) as saved:
            connection.backup(saved, pages=256, progress=progress, sleep=0.05)
            if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup failed integrity check")
        item.update(backup=str(destination), status="prepared")
        journal()
        connection.execute("BEGIN IMMEDIATE")
        try:
            for change in item["changes"]:
                result = connection.execute(
                    "UPDATE threads SET model_provider=? WHERE id=? AND model_provider=?",
                    (target, change["id"], change["old_provider"]))
                if result.rowcount != 1:
                    raise RuntimeError(f"Thread changed since audit: {change['id']}")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity check failed")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    item["status"] = "applied"
    journal()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--to-provider", help="Target provider; default: current config provider, or openai")
    parser.add_argument("--from-provider", action="append", help="Explicit provider, repeatable; default: missing non-builtin providers")
    parser.add_argument("--thread-id", action="append", type=parse_thread_id,
                        help="Limit to a thread UUID or codex://threads/UUID; repeatable")
    parser.add_argument("--everything", type=Path, default=Path(EVERYTHING))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=Path.cwd() / "codex-provider-audit.json")
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args(argv)
    home = args.home.resolve()
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        report = {"status": "config_error", "home": str(home), "error": str(exc)}
        save_json(args.report, report)
        emit(report)
        return 2
    active_provider = config.get("model_provider", "openai")
    args.to_provider = args.to_provider or active_provider
    configured = set(config.get("model_providers", {})) | BUILTINS
    if args.apply and args.to_provider not in configured:
        parser.error("Target provider is not configured; audit without --apply still works")
    if args.apply and os.name != "nt":
        parser.error("Apply is supported on Windows only")
    selected = set(args.from_provider or [])
    thread_ids = set(args.thread_id or [])
    def should_fix(provider):
        return isinstance(provider, str) and bool(provider) and provider != args.to_provider and (
            provider in selected if selected else provider not in configured)
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "home": str(home),
              "target": args.to_provider, "mode": "apply" if args.apply else "audit",
              "active_provider": active_provider, "target_configured": args.to_provider in configured,
              "thread_ids": sorted(thread_ids), "from_providers": sorted(selected),
              "configured_providers": sorted(configured), "databases": [], "files": [], "errors": []}
    db_rollouts = []
    for path in database_paths(home):
        try:
            item, rollouts = database_audit(path, should_fix, thread_ids)
            report["databases"].append(item)
            db_rollouts.extend(rollouts)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            report["errors"].append({"path": str(path), "error": str(exc)})
    paths = discover_sessions(home, args.everything, db_rollouts)
    if thread_ids:
        referenced = {rollout_path(p) for p in db_rollouts}
        paths = [p for p in paths if p in referenced or any(t in p.name.lower() for t in thread_ids)]
    providers = collections.Counter()
    sessions = []
    scanned_bytes = 0
    for index, path in enumerate(paths, 1):
        try:
            item = scan_session(path, should_fix, args.to_provider)
            sessions.append(item)
            providers.update(item["providers"])
            scanned_bytes += item["bytes"]
            if item["changes"]:
                report["files"].append(item)
        except (OSError, ValueError, RuntimeError) as exc:
            report["errors"].append({"path": str(path), "error": str(exc)})
        if index % 250 == 0:
            emit({"scanned": index, "total": len(paths), "affected_files": len(report["files"])})
    report["issues"] = consistency_issues(report["databases"], sessions)
    present = set(providers)
    for database in report["databases"]:
        present.update(p for p in database["providers"] if p)
    for provider in sorted(present - configured):
        report["issues"].append({"kind": "provider_not_configured", "provider": provider})
    if active_provider not in configured:
        report["issues"].append({"kind": "active_provider_not_configured", "provider": active_provider})
    if not report["databases"]:
        report["issues"].append({"kind": "no_state_database_found"})
    if not paths:
        report["issues"].append({"kind": "no_session_files_found"})
    if thread_ids:
        found = {r["id"] for d in report["databases"] for r in d["threads"]}
        found.update(m["id"] for s in sessions for m in s["metadata"] if m["id"])
        for missing in sorted(thread_ids - found):
            report["issues"].append({"kind": "thread_not_found", "id": missing})
    report["status"] = "audit_error" if report["errors"] else "audit_complete"
    report["summary"] = {
        "scanned_files": len(paths), "scanned_bytes": scanned_bytes, "session_providers": dict(providers),
        "affected_files": len(report["files"]),
        "metadata_changes": sum(len(f["changes"]) for f in report["files"]),
        "database_rows": {d["path"]: len(d["changes"]) for d in report["databases"]},
        "missing_providers": sorted(present - configured),
        "issue_counts": dict(collections.Counter(i["kind"] for i in report["issues"])),
        "errors": len(report["errors"]),
    }
    save_json(args.report, report)
    emit(report["summary"])
    if report["errors"]:
        emit({"error": "Audit errors: no changes applied", "report": str(args.report)})
        return 2
    if not args.apply or not (report["files"] or any(d["changes"] for d in report["databases"])):
        return 0
    backup_root = (args.backup_root or home / "provider-repair-backups").resolve()
    backup = backup_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    needed = sum(f["bytes"] for f in report["files"]) * 2
    needed += sum(Path(d["path"]).stat().st_size * 3 for d in report["databases"])
    needed += 100 * 1024 * 1024
    backup_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(backup_root).free < needed:
        raise RuntimeError("Insufficient free space for backups and temporary files")
    backup.mkdir()
    report["backup_directory"] = str(backup)
    manifest = backup / "manifest.json"
    def journal():
        save_json(manifest, report)
    journal()
    try:
        # Repair metadata before DB references. Each unit has its own recoverable journal.
        for index, item in enumerate(report["files"], 1):
            apply_session(item, home, backup, should_fix, args.to_provider, journal)
            if index % 100 == 0:
                emit({"repaired_files": index, "total": len(report["files"])})
        for item in report["databases"]:
            if item["changes"]:
                apply_database(item, home, backup, args.to_provider, journal)
        report["status"] = "applied"
    except BaseException as exc:
        report["status"] = "incomplete"
        report["apply_error"] = str(exc)
        journal()
        save_json(args.report, report)
        raise
    journal()
    save_json(args.report, report)
    emit({"status": report["status"], "backup_directory": str(backup), "report": str(args.report)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
