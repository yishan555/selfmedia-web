"""Run local regression suites behind an explicit test-only Python boundary."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from harness_support import (BOOTSTRAP, ROOT, isolated_environment,
                             new_run_directory, protected_paths, snapshot)

SUITES = (
    "test_portable_assets.py", "test_portable_skills.py",
    "test_workbench_files.py", "test_workbench_web.py",
    "test_native_automations.py", "test_native_packets.py", "test_archive_sync.py",
    "test_harness.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", help="Test filename (repeatable)")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out", help="Optional JSON result file, under repository tmp/")
    parser.add_argument("--source-root", help="Read-only application imports from this repository/main worktree")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    destination = Path(args.out).resolve() if args.out else None
    if destination and not destination.is_relative_to(ROOT / "tmp"):
        parser.error("--out must be inside repository tmp/")
    protected = protected_paths()
    before = snapshot(protected)
    run_root = new_run_directory()
    names = args.suite or list(SUITES)
    results = []
    for name in names:
        suite = ROOT / "tests" / name
        if name not in SUITES or not suite.is_file() or suite.is_symlink():
            results.append({"suite": name, "ok": False, "error": "unknown_suite"})
            continue
        run_dir = new_run_directory(suite.stem, root=run_root)
        environment = isolated_environment(run_dir)
        for directory in (run_dir / "home", run_dir / "data", run_dir / "temp"):
            directory.mkdir()
        try:
            command = [sys.executable, "-B", str(BOOTSTRAP), "--run-dir", str(run_dir),
                       "--target", str(suite)]
            if args.source_root:
                command.extend(["--source-root", str(Path(args.source_root).resolve())])
            completed = subprocess.run(
                command, cwd=run_dir, env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=args.timeout,
            )
            result = {"suite": suite.name, "ok": completed.returncode == 0,
                      "exit_code": completed.returncode}
            output = completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as exc:
            result = {"suite": suite.name, "ok": False, "error": "suite_timeout"}
            output = f"{exc}\n{exc.stdout!r}\n{exc.stderr!r}"
        except OSError as exc:
            result = {"suite": suite.name, "ok": False, "error": "suite_launch_failed"}
            output = type(exc).__name__
        receipt = run_dir / "boundary.json"
        result["boundary_loaded"] = receipt.is_file()
        result["ok"] = result["ok"] and result["boundary_loaded"]
        logfile = run_dir / "output.log"
        logfile.write_text(output, encoding="utf-8")
        result.update({"log": str(logfile), "run_dir": str(run_dir)})
        results.append(result)
        print(f"{'PASS' if result['ok'] else 'FAIL'} {suite.name}: {logfile}",
              file=sys.stderr, flush=True)
    after = snapshot(protected)
    changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    db_changed = any(Path(path).name in {"selfmedia.db", "selfmedia.db-wal", "selfmedia.db-shm"} for path in changed)
    summary = {"schema_version": 1,
               "ok": bool(results) and all(item["ok"] for item in results) and not changed,
               "suite_count": len(results), "failed": sum(not item["ok"] for item in results),
               "repository_db_unchanged": not db_changed,
               "protected_paths_unchanged": not changed,
               "protected_roots": [str(path) for path in protected],
               "protected_changes": changed, "run_dir": str(run_root), "results": results,
               "source_root": str(Path(args.source_root).resolve()) if args.source_root else str(ROOT),
               "scope": "local Python regression boundary, not a production/OS sandbox; no real Hermes/platform validation"}
    (run_root / "protected-before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    (run_root / "protected-after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
