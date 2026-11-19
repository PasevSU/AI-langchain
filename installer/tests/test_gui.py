"""Тестове за таблото за управление на агентите (``gui``)."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pasevsu_installer.config import Config, SuiteSpec
from pasevsu_installer.gui import (
    DASHBOARD_SCHEMA,
    GUI_DIRNAME,
    SNAPSHOT_NAME,
    asset_names,
    build_snapshot,
    make_server,
    write_gui,
    write_snapshot,
)
from pasevsu_installer.state import StateStore

from .support import temp_root, write_text

AGENT_DECLARATION = {
    "schema": "pasevsu.installer.agent.v1",
    "id": "doc-agent",
    "title": "Документационен агент",
    "status": "active",
    "purpose": "Пише документация.",
    "scope": ["docs/"],
    "tasks": [
        {
            "id": "doc-guide",
            "title": "Наръчник",
            "changes": [
                {"op": "write_file_if_absent", "path": "docs/GUIDE.md", "content": "# Ръководство\n"}
            ],
            "verify": [{"op": "assert_file_exists", "path": "docs/GUIDE.md"}],
        }
    ],
}


def make_config(root: Path, *, suites: tuple[SuiteSpec, ...] = ()) -> Config:
    """Минимална конфигурация, насочена към временния корен."""
    return Config(root=root, path=root / "installer" / "installer.config.json", suites=suites)


class EmptySnapshotTest(unittest.TestCase):
    """Таблото работи и когато инсталаторът никога не е пускан."""

    def test_has_schema_and_defaults(self) -> None:
        with temp_root() as root:
            snapshot = build_snapshot(make_config(root))
            self.assertEqual(snapshot["schema"], DASHBOARD_SCHEMA)
            self.assertEqual(snapshot["run"]["phase"], "new")
            self.assertEqual(snapshot["run"]["runs"], 0)
            self.assertEqual(snapshot["agents"], [])
            self.assertEqual(snapshot["tasks"], [])
            self.assertEqual(snapshot["publish"], {})
            self.assertEqual(snapshot["journal"], [])
            self.assertEqual(snapshot["report_markdown"], "")
            self.assertIn("generated_at", snapshot)

    def test_missing_registry_is_reported_not_raised(self) -> None:
        with temp_root() as root:
            snapshot = build_snapshot(make_config(root))
            self.assertIn("registry_error", snapshot)


class PopulatedSnapshotTest(unittest.TestCase):
    """Готов пуск се вижда изцяло в snapshot-а."""

    def _prepare(self, root: Path) -> Config:
        write_text(
            root,
            "installer/agents/doc-agent.json",
            json.dumps(AGENT_DECLARATION, ensure_ascii=False, indent=2) + "\n",
        )
        store = StateStore(root)
        store.ensure()
        state = store.fresh("0.1.6")
        state["phase"] = "done"
        state["runs"] = 2
        state["tasks"] = {
            "doc-guide": {
                "status": "applied",
                "runs": 2,
                "agent": "doc-agent",
                "title": "Наръчник",
                "fingerprint": "abc",
                "history": [{"at": "2026-11-19T10:00:00Z", "status": "applied"}],
            },
            "gone-task": {"status": "applied", "runs": 1, "agent": "old-agent"},
        }
        state["suites"] = [{"id": "installer-unit", "status": "passed", "duration_ms": 1200}]
        store.save(state)
        store.artifacts("publish.json").write_text(
            json.dumps(
                {
                    "status": "published",
                    "branch": "installer/0.1.6",
                    "commit": "deadbeef",
                    "tag": "installer-v0.1.6",
                    "paths": ["installer/VERSION"],
                    "pushed": ["origin"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        store.artifacts("report.md").write_text("# Отчет\n", encoding="utf-8")
        return make_config(
            root,
            suites=(SuiteSpec(id="installer-unit", command=("python", "-m", "unittest")),),
        )

    def test_agents_and_flat_tasks(self) -> None:
        with temp_root() as root:
            config = self._prepare(root)
            snapshot = build_snapshot(config)
            self.assertNotIn("registry_error", snapshot)
            agents = snapshot["agents"]
            self.assertEqual([agent["id"] for agent in agents], ["doc-agent"])
            self.assertEqual(agents[0]["tasks"][0]["state"]["status"], "applied")
            self.assertEqual(agents[0]["tasks"][0]["state"]["runs"], 2)
            flat = {row["id"]: row for row in snapshot["tasks"]}
            self.assertEqual(flat["doc-guide"]["status"], "applied")
            self.assertEqual(flat["doc-guide"]["runs"], 2)
            self.assertEqual(flat["doc-guide"]["at"], "2026-11-19T10:00:00Z")
            self.assertIn("gone-task", flat)
            self.assertEqual(flat["gone-task"]["status"], "applied")

    def test_run_suites_publish_and_report(self) -> None:
        with temp_root() as root:
            config = self._prepare(root)
            snapshot = build_snapshot(config)
            self.assertEqual(snapshot["run"]["phase"], "done")
            self.assertEqual(snapshot["run"]["runs"], 2)
            self.assertEqual(snapshot["suites"][0]["id"], "installer-unit")
            self.assertEqual(snapshot["suites"][0]["status"], "passed")
            self.assertTrue(snapshot["suites"][0]["required"])
            self.assertEqual(snapshot["publish"]["branch"], "installer/0.1.6")
            self.assertEqual(snapshot["report_markdown"], "# Отчет\n")
            artefacts = {row["name"]: row for row in snapshot["artefacts"]}
            self.assertTrue(artefacts["publish.json"]["exists"])
            self.assertGreater(artefacts["publish.json"]["size"], 0)
            self.assertFalse(artefacts["backup.json"]["exists"])

    def test_snapshot_is_json_serialisable(self) -> None:
        with temp_root() as root:
            config = self._prepare(root)
            payload = json.dumps(build_snapshot(config), ensure_ascii=False)
            self.assertIn("doc-guide", payload)


class WriteSnapshotTest(unittest.TestCase):
    """Snapshot-ът се записва атомарно и се чете обратно."""

    def test_writes_into_state_directory(self) -> None:
        with temp_root() as root:
            path = write_snapshot(make_config(root))
            self.assertEqual(path.name, SNAPSHOT_NAME)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema"], DASHBOARD_SCHEMA)

    def test_creates_missing_parent_directory(self) -> None:
        with temp_root() as root:
            target = root / "deep" / "nested" / SNAPSHOT_NAME
            write_snapshot(make_config(root), target)
            self.assertTrue(target.is_file())


class WriteGuiTest(unittest.TestCase):
    """Генерирането на таблото копира всички вградени активи."""

    def test_writes_all_assets_and_snapshot(self) -> None:
        with temp_root() as root:
            written = write_gui(make_config(root))
            names = {path.name for path in written}
            self.assertEqual(names, set(asset_names()) | {SNAPSHOT_NAME})
            for path in written:
                self.assertTrue(path.is_file(), path)
            snapshot = json.loads(
                (root / "installer" / GUI_DIRNAME / SNAPSHOT_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(snapshot["schema"], DASHBOARD_SCHEMA)

    def test_is_idempotent(self) -> None:
        with temp_root() as root:
            config = make_config(root)
            first = write_gui(config)
            second = write_gui(config)
            self.assertEqual(len(first), len(second))
            self.assertTrue(all(path.is_file() for path in second))


class AssetNamesTest(unittest.TestCase):
    """Вградените активи съществуват в пакета."""

    def test_contains_the_dashboard_files(self) -> None:
        names = asset_names()
        self.assertIn("index.html", names)
        self.assertIn("app.css", names)
        self.assertIn("app.js", names)


class ServerTest(unittest.TestCase):
    """Локалният сървър сервира страницата и живия snapshot."""

    def test_serves_index_and_snapshot(self) -> None:
        with temp_root() as root:
            server = make_server(make_config(root), port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{int(server.server_address[1])}"
            try:
                with urllib.request.urlopen(f"{base}/", timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/html", response.headers["Content-Type"])
                    self.assertIn("app.js", response.read().decode("utf-8"))
                with urllib.request.urlopen(f"{base}/api/snapshot", timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(payload["schema"], DASHBOARD_SCHEMA)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{base}/nope.txt", timeout=10)
                self.assertEqual(caught.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
