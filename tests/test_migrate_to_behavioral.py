import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


migrate = _load_module("skill_evolver_migrate_to_behavioral",
                       SCRIPTS_DIR / "migrate_to_behavioral.py")


class ClassifyAssertionTests(unittest.TestCase):
    def test_path_hit_is_always_output(self):
        target, _ = migrate.classify_assertion({"type": "path_hit", "value": "references/foo.md"})
        self.assertEqual(target, "output")

    def test_script_check_is_always_output(self):
        target, _ = migrate.classify_assertion({"type": "script_check", "value": "check.py"})
        self.assertEqual(target, "output")

    def test_contains_with_doc_structure_marker_is_skill_doc(self):
        target, reason = migrate.classify_assertion(
            {"type": "contains", "value": "must reference references/foo.md"})
        self.assertEqual(target, "skill_doc")
        self.assertIn("references/", reason)

    def test_contains_readme_marker_is_skill_doc(self):
        target, _ = migrate.classify_assertion(
            {"type": "contains", "value": "See the README for setup"})
        self.assertEqual(target, "skill_doc")

    def test_contains_without_marker_is_output(self):
        target, _ = migrate.classify_assertion(
            {"type": "contains", "value": "MAGIC_WORD_42"})
        self.assertEqual(target, "output")

    def test_not_contains_with_marker_is_skill_doc(self):
        target, _ = migrate.classify_assertion(
            {"type": "not_contains", "value": "agents/legacy_agent.md"})
        self.assertEqual(target, "skill_doc")

    def test_regex_with_marker_is_skill_doc(self):
        target, _ = migrate.classify_assertion(
            {"type": "regex", "value": r"必须包含字段说明.*"})
        self.assertEqual(target, "skill_doc")

    def test_json_schema_defaults_to_output(self):
        target, _ = migrate.classify_assertion({"type": "json_schema", "value": "{}"})
        self.assertEqual(target, "output")

    def test_file_exists_defaults_to_output(self):
        target, _ = migrate.classify_assertion({"type": "file_exists", "value": "foo.txt"})
        self.assertEqual(target, "output")

    def test_marker_match_is_case_insensitive(self):
        target, _ = migrate.classify_assertion(
            {"type": "contains", "value": "check the ReadMe"})
        self.assertEqual(target, "skill_doc")


class MigrateEvalsTests(unittest.TestCase):
    def test_tags_every_assertion_and_collects_skill_doc_entries(self):
        data = {"evals": [
            {"id": "c1", "assertions": [
                {"type": "contains", "value": "references/foo.md"},
                {"type": "contains", "value": "MAGIC"},
            ]},
            {"id": "c2", "assertions": [
                {"type": "path_hit", "value": "references/bar.md"},
            ]},
        ]}
        migrated, skill_doc_entries = migrate.migrate_evals(data)

        c1_assertions = migrated["evals"][0]["assertions"]
        self.assertEqual(c1_assertions[0]["target"], "skill_doc")
        self.assertEqual(c1_assertions[1]["target"], "output")
        c2_assertions = migrated["evals"][1]["assertions"]
        self.assertEqual(c2_assertions[0]["target"], "output")

        self.assertEqual(len(skill_doc_entries), 1)
        self.assertEqual(skill_doc_entries[0]["case_id"], "c1")
        self.assertEqual(skill_doc_entries[0]["assertion_index"], 0)

    def test_handles_bare_list_format_not_just_evals_key(self):
        data = [{"id": "c1", "assertions": [{"type": "contains", "value": "x"}]}]
        migrated, _ = migrate.migrate_evals(data)
        self.assertEqual(migrated[0]["assertions"][0]["target"], "output")

    def test_empty_evals_produces_no_skill_doc_entries(self):
        migrated, skill_doc_entries = migrate.migrate_evals({"evals": []})
        self.assertEqual(skill_doc_entries, [])


class BuildMigrationReportTests(unittest.TestCase):
    def test_report_lists_all_skill_doc_entries(self):
        entries = [{"case_id": "c1", "assertion_index": 0, "type": "contains",
                    "value": "references/foo.md", "reason": "doc marker"}]
        report = migrate.build_migration_report(entries, Path("evals.json"))
        self.assertIn("c1", report)
        self.assertIn("references/foo.md", report)
        self.assertIn("1 assertion(s)", report)

    def test_report_handles_zero_entries(self):
        report = migrate.build_migration_report([], Path("evals.json"))
        self.assertIn("none", report)


class MainCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_does_not_overwrite_input_file(self):
        evals_path = self.tmp / "evals.json"
        original = {"evals": [{"id": "c1", "assertions": [
            {"type": "contains", "value": "references/foo.md"}]}]}
        evals_path.write_text(json.dumps(original))

        rc = migrate.main([str(evals_path)])
        self.assertEqual(rc, 0)

        # Original untouched — no "target" field leaked into it.
        untouched = json.loads(evals_path.read_text())
        self.assertNotIn("target", untouched["evals"][0]["assertions"][0])

        migrated_path = self.tmp / "evals.migrated.json"
        self.assertTrue(migrated_path.exists())
        migrated = json.loads(migrated_path.read_text())
        self.assertEqual(migrated["evals"][0]["assertions"][0]["target"], "skill_doc")

        report_path = self.tmp / "migration_report.md"
        self.assertTrue(report_path.exists())
        self.assertIn("c1", report_path.read_text())

    def test_missing_input_file_returns_nonzero(self):
        rc = migrate.main([str(self.tmp / "does_not_exist.json")])
        self.assertEqual(rc, 1)

    def test_custom_out_path_is_respected(self):
        evals_path = self.tmp / "evals.json"
        evals_path.write_text(json.dumps({"evals": []}))
        custom_out = self.tmp / "custom.json"

        rc = migrate.main([str(evals_path), "--out", str(custom_out)])
        self.assertEqual(rc, 0)
        self.assertTrue(custom_out.exists())
        self.assertTrue((self.tmp / "migration_report.md").exists())


if __name__ == "__main__":
    unittest.main()
