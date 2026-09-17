import csv
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

import action_file_formats as aff
import dashboard_controller as dc


class ActionFileFormatTests(unittest.TestCase):
    def test_default_is_csv(self):
        self.assertEqual(aff.action_file_format({}), "csv")

    def test_xlsx_and_xls_alias(self):
        self.assertEqual(aff.action_file_format({"action_files": {"format": "xlsx"}}), "xlsx")
        self.assertEqual(aff.action_file_format({"action_files": {"format": "xls"}}), "xlsx")

    def test_csv_to_xlsx_is_real_workbook(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "in.csv"
            dst = root / "out.xlsx"
            src.write_text("id,value\n00123,hello\n", encoding="utf-8")
            aff.csv_to_xlsx(src, dst)
            self.assertTrue(zipfile.is_zipfile(dst))
            with zipfile.ZipFile(dst) as zf:
                self.assertIn("xl/worksheets/sheet1.xml", zf.namelist())
                xml = zf.read("xl/worksheets/sheet1.xml").decode()
                self.assertIn("00123", xml)


class ControllerTests(unittest.TestCase):
    def test_examples_validate(self):
        repo = Path(__file__).resolve().parents[1]
        for project in sorted((repo / "dashboards").iterdir()):
            if not project.is_dir():
                continue
            cfg = yaml.safe_load((project / "dashboard.yml").read_text())
            dc.validate_config(project, cfg)

    def test_discovery_finds_two_examples(self):
        repo = Path(__file__).resolve().parents[1]
        names = [p.name for p in dc.discover_projects(repo / "dashboards", "dashboard.yml")]
        self.assertEqual(names, ["Example_Alpha", "Example_Beta"])

    def test_local_xlsx_delivery_and_archive(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            output = project / "docs"
            exports = project / "exports"
            output.mkdir(parents=True)
            exports.mkdir()
            (output / "index.html").write_text("hello")
            with (exports / "list.csv").open("w", newline="") as f:
                csv.writer(f).writerows([["id", "value"], ["001", "x"]])
            cfg = {
                "project": {"name": "Test"},
                "delivery": {"mode": "local", "local": {"root": "Outputs"}, "archive": {"enabled": True}},
                "dashboard": {"folder": "Dashboard"},
                "action_files": {"format": "xlsx"},
                "targets": {"Team": {"folder": "Team"}},
                "products": {"list": {"file": "exports/list.csv", "targets": ["Team"]}},
            }
            rows = dc.distribute(project, output, cfg, dc.logging.getLogger("test"), None)
            self.assertTrue((project / "Outputs" / "Dashboard" / "index.html").is_file())
            current = project / "Outputs" / "Team" / "Current" / "list.xlsx"
            self.assertTrue(current.is_file())
            self.assertTrue(all(r.status == "success" for r in rows))

            # A changed second delivery archives the previous Current set.
            with (exports / "list.csv").open("w", newline="") as f:
                csv.writer(f).writerows([["id", "value"], ["002", "changed"]])
            rows2 = dc.distribute(project, output, cfg, dc.logging.getLogger("test"), None)
            archives = list((project / "Outputs" / "Team" / "Archive").glob("*.zip"))
            self.assertEqual(len(archives), 1)
            self.assertTrue(all(r.status == "success" for r in rows2))


if __name__ == "__main__":
    unittest.main()
