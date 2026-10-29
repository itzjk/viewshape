import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from viewshape.cli import main, render
from viewshape.compare import Limits, compare, inspect_snapshot
from viewshape.snapshot import InvalidInput, copy_snapshot


ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.before = self.directory / "before.sqlite"
        self.after = self.directory / "after.sqlite"

    def tearDown(self):
        self.temporary.cleanup()

    def database(self, path, sql, setup=None):
        connection = sqlite3.connect(path)
        try:
            if setup:
                setup(connection)
            connection.executescript(sql)
            connection.commit()
        finally:
            connection.close()

    def pair(self, before, after=None):
        self.database(self.before, before)
        self.database(self.after, before if after is None else after)
        return compare(self.before, self.after)

    def view(self, report, name="v"):
        return next(view for view in report["views"] if view["name"] == name)

    def test_star_changes_while_sql_and_explicit_projection_stay_identical(self):
        views = "CREATE VIEW v AS SELECT * FROM items; CREATE VIEW explicit AS SELECT id,title FROM items;"
        report = self.pair("CREATE TABLE items(id INTEGER,title TEXT);" + views,
                           "CREATE TABLE items(id INTEGER,title TEXT,state TEXT);" + views)
        self.assertEqual(report["exit_code"], 1)
        star = self.view(report)
        self.assertEqual(star["status"], "definition-unchanged/output-changed")
        self.assertEqual(star["before"]["sql"], star["after"]["sql"])
        self.assertEqual([c["name"] for c in star["after"]["columns"]], ["id", "title", "state"])
        self.assertEqual(star["changes"], [{"kind": "column-added", "after": {
            "position": 2, "name": "state", "declared_type": "TEXT"}}])
        self.assertEqual(self.view(report, "explicit")["status"], "unchanged")

    def test_order_is_significant_even_when_definition_stays_identical(self):
        report = self.pair("CREATE TABLE t(a TEXT,b TEXT); CREATE VIEW v AS SELECT * FROM t;",
                           "CREATE TABLE t(b TEXT,a TEXT); CREATE VIEW v AS SELECT * FROM t;")
        view = self.view(report)
        self.assertEqual(view["status"], "definition-unchanged/output-changed")
        self.assertEqual([c["kind"] for c in view["changes"]], ["position-changed", "position-changed"])
        self.assertEqual(view["after"]["columns"][0]["name"], "b")

    def test_no_rename_inference(self):
        report = self.pair("CREATE TABLE t(old TEXT); CREATE VIEW v AS SELECT * FROM t;",
                           "CREATE TABLE t(new TEXT); CREATE VIEW v AS SELECT * FROM t;")
        self.assertEqual([c["kind"] for c in self.view(report)["changes"]], ["column-removed", "column-added"])

    def test_declared_type_changes(self):
        report = self.pair("CREATE TABLE t(a TEXT); CREATE VIEW v AS SELECT * FROM t;",
                           "CREATE TABLE t(a INTEGER); CREATE VIEW v AS SELECT * FROM t;")
        self.assertEqual(self.view(report)["changes"], [{"kind": "declared-type-changed", "name": "a", "before": "TEXT", "after": "INTEGER"}])

    def test_empty_type_and_expression_type_are_engine_metadata(self):
        report = self.pair("CREATE TABLE t(a); CREATE VIEW v AS SELECT a, a+1 AS derived FROM t;")
        self.assertEqual(report["exit_code"], 0)
        with contextlib.closing(sqlite3.connect(self.before)) as connection:
            expected = [row[0] for row in connection.execute("SELECT type FROM pragma_table_xinfo('v') ORDER BY cid")]
        self.assertEqual([c["declared_type"] for c in self.view(report)["before"]["columns"]], expected)
        self.assertEqual(expected[1], "")

    def test_nested_view_observes_change_without_claiming_cause(self):
        suffix = "CREATE VIEW base AS SELECT * FROM t; CREATE VIEW v AS SELECT * FROM base;"
        report = self.pair("CREATE TABLE t(a);" + suffix, "CREATE TABLE t(a,b);" + suffix)
        self.assertEqual(self.view(report)["status"], "definition-unchanged/output-changed")
        self.assertNotIn("cause", self.view(report))

    def test_added_and_removed_views(self):
        report = self.pair("CREATE VIEW gone AS SELECT 1 AS x;", "CREATE VIEW added AS SELECT 1 AS x;")
        self.assertEqual([(v["name"], v["status"]) for v in report["views"]], [("added", "view-added"), ("gone", "view-removed")])

    def test_definition_change_does_not_claim_equivalent_results(self):
        report = self.pair("CREATE VIEW v AS SELECT 1 AS x;", "CREATE VIEW v AS SELECT 2 AS x;")
        self.assertEqual(self.view(report)["status"], "definition-changed/output-unchanged")
        self.assertEqual(report["exit_code"], 1)

    def test_quoted_unicode_names_and_terminal_control_are_safe(self):
        name = 'space " café\x1b[31m'
        quoted = '"' + name.replace('"', '""') + '"'
        report = self.pair(f'CREATE TABLE t("naïve name" TEXT); CREATE VIEW {quoted} AS SELECT * FROM t;')
        self.assertEqual(self.view(report, name)["before"]["columns"][0]["name"], "naïve name")
        output, _ = render(report, False, 2097152)
        self.assertNotIn("\x1b", output)
        self.assertIn("\\u001b", output)

    def test_missing_table_and_column_are_per_view_per_side(self):
        report = self.pair("CREATE TABLE t(a); CREATE VIEW bad_table AS SELECT * FROM missing; CREATE VIEW bad_column AS SELECT b FROM t; CREATE VIEW good AS SELECT a FROM t;")
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(self.view(report, "good")["status"], "unchanged")
        for name in ("bad_table", "bad_column"):
            view = self.view(report, name)
            self.assertEqual(view["status"], "unverified")
            for side in ("before", "after"):
                self.assertEqual(view[side]["status"], "unresolvable")
                self.assertIn("no such", view[side]["error"]["message"])
                self.assertIsNone(view[side]["columns"])
            self.assertEqual(view["changes"], [])

    def test_difference_and_error_exit_precedence(self):
        report = self.pair("CREATE VIEW bad AS SELECT * FROM missing; CREATE VIEW v AS SELECT 1 AS x;",
                           "CREATE VIEW bad AS SELECT * FROM missing; CREATE VIEW v AS SELECT 2 AS x;")
        self.assertEqual(report["exit_code"], 2)
        self.assertTrue(report["differences_observed"])
        self.assertEqual(self.view(report)["status"], "definition-changed/output-unchanged")

    def test_unknown_function_requires_environment(self):
        report = self.pair("CREATE VIEW v AS SELECT app_function(1) AS x; CREATE VIEW good AS SELECT 1 AS x;")
        self.assertEqual(self.view(report)["before"]["status"], "environment-required")
        self.assertEqual(self.view(report, "good")["status"], "unchanged")
        self.assertEqual(report["exit_code"], 2)

    def test_unknown_collation_requires_environment(self):
        report = self.pair("CREATE TABLE t(a); CREATE VIEW v AS SELECT a FROM t ORDER BY a COLLATE app_sort;")
        self.assertEqual(self.view(report)["before"]["status"], "environment-required")
        self.assertIn("collation", self.view(report)["before"]["error"]["message"])

    def test_virtual_table_dependency_is_unsupported(self):
        try:
            report = self.pair("CREATE VIRTUAL TABLE docs USING fts5(body); CREATE VIEW v AS SELECT body FROM docs;")
        except sqlite3.OperationalError:
            self.skipTest("FTS5 unavailable")
        self.assertEqual(self.view(report)["before"]["status"], "unsupported")
        self.assertEqual(report["exit_code"], 2)

    def test_unknown_virtual_module(self):
        sql = "CREATE VIRTUAL TABLE module_table USING fts5(body); CREATE VIEW v AS SELECT * FROM module_table;"
        try:
            self.database(self.before, sql)
        except sqlite3.OperationalError:
            self.skipTest("FTS5 unavailable")
        with contextlib.closing(sqlite3.connect(self.before)) as connection:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute("UPDATE sqlite_schema SET sql=replace(sql,'fts5','absent_module') WHERE name='module_table'")
            connection.commit()
        shutil.copyfile(self.before, self.after)
        report = compare(self.before, self.after)
        self.assertEqual(self.view(report)["before"]["status"], "unsupported")

    def test_eponymous_virtual_table_is_unsupported(self):
        report = self.pair("CREATE VIEW v AS SELECT key,value FROM json_each('[1,2]');")
        self.assertEqual(self.view(report)["before"]["status"], "unsupported")
        self.assertEqual(report["exit_code"], 2)

    def test_sqlite_schema_aliases_are_valid_view_dependencies(self):
        report = self.pair("CREATE VIEW v AS SELECT name FROM sqlite_schema; CREATE VIEW alias AS SELECT name FROM sqlite_master;")
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(all(view["before"]["status"] == "resolved" for view in report["views"]))

    def test_zero_views_is_empty_not_certified(self):
        report = self.pair("CREATE TABLE t(a);")
        self.assertEqual(report["status"], "empty")
        self.assertTrue(report["empty"])
        self.assertEqual(report["exit_code"], 0)

    def test_missing_file_is_not_created(self):
        self.database(self.after, "CREATE TABLE t(a);")
        report = compare(self.before, self.after)
        self.assertEqual(report["exit_code"], 3)
        self.assertFalse(self.before.exists())

    def test_non_sqlite_and_zero_byte_input(self):
        self.database(self.after, "CREATE TABLE t(a);")
        for content in (b"", b"not sqlite"):
            self.before.write_bytes(content)
            self.assertEqual(compare(self.before, self.after)["exit_code"], 3)

    def test_directory_input(self):
        self.assertEqual(compare(self.directory, self.directory)["exit_code"], 3)

    def test_max_bytes_rejects_before_opening_sqlite(self):
        self.pair("CREATE TABLE t(a);")
        report = compare(self.before, self.after, Limits(max_bytes=1))
        self.assertEqual(report["exit_code"], 3)
        self.assertIn("max-bytes", report["errors"][0]["message"])

    def test_object_limit_is_incomplete_and_does_not_infer_absence(self):
        self.pair("CREATE VIEW v AS SELECT 1 AS x; CREATE VIEW other AS SELECT 1 AS x;",
                  "CREATE VIEW v AS SELECT 1 AS x;")
        report = compare(self.before, self.after, Limits(max_objects=1))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(self.view(report)["status"], "unverified")
        self.assertFalse(report["differences_observed"])
        self.assertEqual(report["snapshots"]["before"]["coverage"]["views_omitted"], 2)

    def test_output_limit_never_reports_match(self):
        self.pair("".join(f"CREATE VIEW v{i} AS SELECT 1 AS x;" for i in range(40)))
        report = compare(self.before, self.after, Limits(max_output_bytes=4096))
        output, code = render(report, True, 4096)
        self.assertEqual(code, 2)
        self.assertLessEqual(len(output.encode()), 4096)
        self.assertEqual(json.loads(output)["status"], "incomplete")

    def test_render_limit_omits_explicitly(self):
        report = self.pair("CREATE VIEW v AS SELECT '" + "x" * 1500 + "' AS x;")
        output, code = render(report, True, 4096)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["report_views_omitted"], 1)

    def test_nonempty_sidecars_are_rejected_and_originals_untouched(self):
        self.pair("CREATE VIEW v AS SELECT 1 AS x;")
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(self.before) + suffix)
            sidecar.write_bytes(b"pending")
            digest = hashlib.sha256(self.before.read_bytes()).hexdigest()
            report = compare(self.before, self.after)
            self.assertEqual(report["exit_code"], 3)
            self.assertIn(suffix, report["errors"][0]["message"])
            self.assertEqual(sidecar.read_bytes(), b"pending")
            self.assertEqual(hashlib.sha256(self.before.read_bytes()).hexdigest(), digest)
            sidecar.unlink()

    def test_live_wal_is_rejected(self):
        connection = sqlite3.connect(self.before)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE t(a)")
            connection.commit()
            self.database(self.after, "CREATE TABLE t(a);")
            self.assertEqual(compare(self.before, self.after)["exit_code"], 3)
        finally:
            connection.close()

    def test_hashes_and_source_directories_unchanged_including_zero_sidecars(self):
        self.pair("CREATE TABLE t(a); INSERT INTO t VALUES('secret row'); CREATE VIEW v AS SELECT * FROM t;")
        Path(str(self.before) + "-wal").touch()
        Path(str(self.before) + "-journal").touch()
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.directory.iterdir()}
        report = compare(self.before, self.after)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.directory.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(report["snapshots"]["before"]["sha256"], before[self.before.name])
        self.assertNotIn("secret row", json.dumps(report))

    def test_volatile_builtin_and_load_extension_are_never_executed(self):
        report = self.pair("CREATE VIEW v AS SELECT abs(-9223372036854775808) AS overflow; CREATE VIEW ext AS SELECT load_extension('/missing') AS x;")
        self.assertEqual(self.view(report)["before"]["status"], "resolved")
        self.assertEqual(self.view(report, "ext")["before"]["status"], "environment-required")

    def test_sql_limit_keeps_previous_view_result(self):
        self.pair("CREATE VIEW a_good AS SELECT 1 AS x; CREATE VIEW z_large AS SELECT '" + "x" * 270000 + "' AS x;")
        report = compare(self.before, self.after)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(self.view(report, "a_good")["status"], "unchanged")
        self.assertEqual(self.view(report, "z_large")["before"]["status"], "limit-exceeded")

    def test_deadline_and_unstable_copy_are_rejected(self):
        self.database(self.before, "CREATE TABLE t(a);")
        with self.assertRaisesRegex(InvalidInput, "timeout"):
            copy_snapshot(self.before, self.directory / "copy.sqlite", 100000, 0)
        original = Path.open
        reads = [0]
        changing_path = self.before.resolve()

        class ChangingReader:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def __getattr__(self, name):
                return getattr(self.file, name)

            def read(self, size):
                data = self.file.read(size)
                reads[0] += 1
                if reads[0] == 2:
                    with original(changing_path, "r+b") as writer:
                        writer.seek(100)
                        writer.write(b"changed!")
                return data

        def opening(path, mode="r", *args, **kwargs):
            file = original(path, mode, *args, **kwargs)
            return ChangingReader(file) if path == changing_path and mode == "rb" else file

        with patch.object(Path, "open", opening):
            with self.assertRaisesRegex(InvalidInput, "changed"):
                copy_snapshot(self.before, self.directory / "unstable.sqlite", 100000, time.monotonic() + 5)

    def test_worker_watchdog_preserves_checkpointed_results(self):
        script = "import json,sys,time; json.load(sys.stdin); print(json.dumps({'event':'inventory','objects_total':1,'views_total':1}),flush=True); print(json.dumps({'event':'view','name':'v','sql':'CREATE VIEW v AS SELECT 1','status':'resolved','columns':[]}),flush=True); time.sleep(10)"
        real_popen = subprocess.Popen

        def stalled(*args, **kwargs):
            return real_popen([sys.executable, "-c", script], **kwargs)

        with patch("viewshape.compare.subprocess.Popen", side_effect=stalled):
            result = inspect_snapshot(self.before, Limits(timeout=0.4), self.directory, "watchdog")
        self.assertIn("v", result["views"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["message"], "Inspection timed out")

    def test_engine_minimum_is_checked(self):
        with patch("viewshape.compare.sqlite3.sqlite_version_info", (3, 36, 0)):
            report = compare(self.before, self.after)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["errors"][0]["category"], "unsupported")

    def test_worker_never_imports_project_packages_from_cwd_or_pythonpath(self):
        self.pair("CREATE VIEW v AS SELECT 1 AS x;")
        shadow = self.directory / "project"
        package = shadow / "viewshape"
        package.mkdir(parents=True)
        marker = shadow / "EXECUTED"
        (package / "__init__.py").write_text(
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\nraise RuntimeError('project code loaded')\n"
        )
        previous = Path.cwd()
        try:
            os.chdir(shadow)
            with patch.dict(os.environ, {"PYTHONPATH": str(shadow)}):
                report = compare(self.before, self.after)
        finally:
            os.chdir(previous)
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(marker.exists())

    def test_large_user_file_budget_does_not_overflow_sqlite_limits(self):
        self.pair("CREATE VIEW v AS SELECT 1 AS x;")
        report = compare(self.before, self.after, Limits(max_bytes=4294967296))
        self.assertEqual(report["exit_code"], 0)

    def test_cli_json_and_exit_codes(self):
        self.pair("CREATE VIEW v AS SELECT 1 AS x;")
        result = subprocess.run([sys.executable, "-m", "viewshape", str(self.before), str(self.after), "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["format_version"], 1)
        for args in (["--timeout", "nan"], ["--timeout", "301"], ["--max-objects", "0"], ["--max-objects", "1" + "0" * 4000], ["--max-output-bytes", "1"], []):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main([str(self.before), str(self.after)] + args if args else [])
            self.assertEqual(raised.exception.code, 3)

    def test_demo_snapshots_and_integrity(self):
        paths = [ROOT / "examples" / "snapshots" / (side + ".sqlite") for side in ("before", "after")]
        for path in paths:
            with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        report = compare(*paths)
        self.assertEqual(self.view(report, "public_items")["status"], "definition-unchanged/output-changed")
        self.assertEqual(self.view(report, "explicit_items")["status"], "unchanged")


if __name__ == "__main__":
    unittest.main()
