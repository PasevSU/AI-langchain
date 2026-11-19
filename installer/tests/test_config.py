"""Tests for configuration loading and suite declarations."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pasevsu_installer.config import CONFIG_SCHEMA, Config, load_config
from pasevsu_installer.errors import ConfigError

from .support import CONFIG_PATH, INSTALLER_DIR, dump_json, temp_root

REPO_ROOT = INSTALLER_DIR.parent


def _write_config(root: Path, payload: dict) -> None:
    dump_json(root, "installer/installer.config.json", payload)


class ShippedConfigTest(unittest.TestCase):
    """The configuration committed to the repository must be usable as-is."""

    def test_shipped_config_loads(self) -> None:
        config = load_config(REPO_ROOT, CONFIG_PATH)
        self.assertEqual(config.name, "pasevsu-installer-agent")
        self.assertEqual(config.commit_scope, "installer")
        self.assertEqual(config.tag_prefix, "installer-v")
        self.assertTrue(config.publish_enabled)
        self.assertFalse(config.allow_master)
        ids = [suite.id for suite in config.suites]
        self.assertIn("installer-unit", ids)
        self.assertIn("installer-compile", ids)
        unit = next(suite for suite in config.suites if suite.id == "installer-unit")
        self.assertTrue(unit.required)
        self.assertIn("unittest", unit.command)

    def test_version_file_is_read(self) -> None:
        config = load_config(REPO_ROOT, CONFIG_PATH)
        self.assertRegex(config.read_version(), r"^\d+\.\d+\.\d+$")

    def test_every_suite_has_a_reason_or_a_tool(self) -> None:
        config = load_config(REPO_ROOT, CONFIG_PATH)
        for suite in config.suites:
            self.assertTrue(suite.command, suite.id)
            if suite.requires:
                self.assertTrue(suite.skip_reason, suite.id)


class ValidationTest(unittest.TestCase):
    """Invalid configuration fails loudly, never silently."""

    def test_missing_file(self) -> None:
        with temp_root() as root, self.assertRaises(ConfigError):
            load_config(root)

    def test_bad_json(self) -> None:
        with temp_root() as root:
            path = root / "installer" / "installer.config.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_wrong_schema(self) -> None:
        with temp_root() as root:
            _write_config(root, {"schema": "other.v1"})
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_suite_without_id(self) -> None:
        with temp_root() as root:
            _write_config(
                root, {"schema": CONFIG_SCHEMA, "test_suites": [{"command": ["true"]}]}
            )
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_suite_with_empty_command(self) -> None:
        with temp_root() as root:
            _write_config(root, {"schema": CONFIG_SCHEMA, "test_suites": [{"id": "x"}]})
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_suite_with_string_command(self) -> None:
        with temp_root() as root:
            _write_config(
                root,
                {"schema": CONFIG_SCHEMA, "test_suites": [{"id": "x", "command": "true"}]},
            )
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_coercion_of_suite_fields(self) -> None:
        with temp_root() as root:
            _write_config(
                root,
                {
                    "schema": CONFIG_SCHEMA,
                    "test_suites": [
                        {
                            "id": "unit",
                            "command": ["python", "-m", "unittest"],
                            "requires": ["python"],
                            "timeout_s": "120",
                        }
                    ],
                },
            )
            config = load_config(root)
            suite = config.suites[0]
            self.assertEqual(suite.command, ("python", "-m", "unittest"))
            self.assertEqual(suite.requires, ("python",))
            self.assertEqual(suite.timeout_s, 120)

    def test_publish_block_is_honoured(self) -> None:
        with temp_root() as root:
            _write_config(
                root,
                {
                    "schema": CONFIG_SCHEMA,
                    "publish": {"enabled": False, "push": False, "allow_master": True},
                },
            )
            config = load_config(root)
            self.assertFalse(config.publish_enabled)
            self.assertFalse(config.push)
            self.assertTrue(config.allow_master)


class ConfigDefaultsTest(unittest.TestCase):
    """Defaults keep the installer inside its own directories."""

    def test_defaults(self) -> None:
        with temp_root() as root:
            config = Config(root=root, path=root / "installer/installer.config.json")
            self.assertEqual(config.state_dir, ".installer")
            self.assertEqual(config.registry_dir, "installer/agents")
            self.assertEqual(config.backup_tag_prefix, "installer-backup")
            self.assertEqual(config.read_version(), "0.0.0")
            self.assertEqual(config.suites, ())
            self.assertEqual(config.to_dict()["name"], "pasevsu-installer-agent")

    def test_payload_is_json_safe(self) -> None:
        with temp_root() as root:
            _write_config(root, {"schema": CONFIG_SCHEMA, "test_suites": []})
            payload = load_config(root).to_dict()
            self.assertEqual(payload["suites"], [])
            self.assertEqual(json.loads(json.dumps(payload))["commit_scope"], "installer")


if __name__ == "__main__":
    unittest.main()
