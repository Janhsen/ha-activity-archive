"""Start checks, repair issues, services, set-up and static files (marker: SYNTHETIC)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from custom_components.activity_archive import (
    CONFIG_SCHEMA,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.activity_archive import issues as issue_mod
from custom_components.activity_archive.core import health
from custom_components.activity_archive.manager import ArchiveError
from custom_components.activity_archive.services import async_register_services
from tests.ha_env import Env

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "activity_archive"
KEY = ("activity_archive", "{}")


class IssueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()
        self.hass = self.env.hass
        self.rec = self.env.recorder

    def tearDown(self):
        self.env.close()

    def ids(self):
        return sorted(k[1] for k in self.hass.issues)

    async def test_clean_setup_has_no_issue(self):
        self.assertEqual(await self.env.manager.async_run_checks(), [])
        self.assertEqual(self.ids(), [])

    async def test_all_findings_and_they_clear_again(self):
        self.rec.keep_days = 10
        self.rec.dialect_name = "sqlite"
        self.rec.entity_filter = lambda e: "heart_rate" not in e
        self.rec.recording = False
        found = await self.env.manager.async_run_checks()
        self.assertEqual(
            sorted(found),
            ["database_backend", "not_recorded", "recorder_not_recording", "retention_too_short"],
        )
        self.assertEqual(self.ids(), sorted(found))
        sev = {k[1]: v["severity"] for k, v in self.hass.issues.items()}
        self.assertEqual(sev["retention_too_short"], "error")
        self.assertEqual(sev["database_backend"], "warning")
        self.assertEqual(
            self.hass.issues[("activity_archive", "not_recorded")]["placeholders"]["count"], "2"
        )
        self.rec.keep_days, self.rec.dialect_name, self.rec.entity_filter, self.rec.recording = (
            7300,
            "postgresql",
            None,
            True,
        )
        await self.env.manager.async_run_checks()
        self.assertEqual(self.ids(), [])

    async def test_low_disk_space(self):
        import shutil
        from unittest import mock

        usage = shutil._ntuple_diskusage(100, 99, 1 * 2**30)
        with mock.patch.object(shutil, "disk_usage", return_value=usage):
            self.assertEqual(await self.env.manager.async_run_checks(), ["low_disk_space"])

    async def test_checks_do_not_change_anything(self):
        self.rec.keep_days = 10
        await self.env.manager.async_run_checks()
        self.assertEqual(self.rec.keep_days, 10)
        self.assertEqual(self.hass.services.calls, [])
        self.assertEqual(self.hass.entity_registry.entities, {})

    async def test_ledger_issue_not_removed_by_sync(self):
        issue_mod.set_ledger_issue(self.hass, 99)
        await self.env.manager.async_run_checks()
        self.assertIn(("activity_archive", "ledger_unreadable"), self.hass.issues)
        issue_mod.clear_ledger_issue(self.hass)
        self.assertEqual(self.ids(), [])

    async def test_filter_issue_covers_exact_entity_list_of_people(self):
        self.rec.entity_filter = lambda e: False
        facts = await issue_mod.async_collect_facts(self.hass, ["sensor.activity_archive_alex"])
        self.assertEqual(facts.unrecorded, ("sensor.activity_archive_alex",))


class SetupAndServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = Env()
        self.hass = self.env.hass

    def tearDown(self):
        self.env.close()

    async def test_setup_registers_five_admin_services_with_responses(self):
        self.assertTrue(await async_setup(self.hass, {}))
        self.assertEqual(
            sorted(self.hass.admin_services),
            ["import_file", "remove_test_state", "rollback", "verify", "write_test_state"],
        )
        self.assertTrue(all(v[2].value == "optional" for v in self.hass.admin_services.values()))
        self.assertEqual(CONFIG_SCHEMA, ("schema", "activity_archive"))

    async def test_setup_entry_creates_folder_and_manager(self):
        import shutil

        shutil.rmtree(self.env.import_dir)
        self.assertTrue(await async_setup_entry(self.hass, self.env.entry))
        self.assertTrue(self.env.import_dir.is_dir())
        self.assertEqual(len(self.env.entry.unload_callbacks), 2)
        self.assertTrue(await async_unload_entry(self.hass, self.env.entry))

    async def test_service_handlers_pass_arguments_and_translate_errors(self):
        from homeassistant.exceptions import ServiceValidationError

        async_register_services(self.hass)
        handler = self.hass.admin_services["import_file"][0]
        self.env.put_fit()
        call = type("Call", (), {"data": {"file": "a.fit", "dry_run": True}})()
        with self.assertRaises(ServiceValidationError) as ctx:
            await handler(call)
        self.assertEqual(ctx.exception.translation_key, "person_required")
        self.assertEqual(ctx.exception.translation_domain, "activity_archive")
        call = type("Call", (), {"data": {"file": "a.fit", "person": "alex", "dry_run": True}})()
        result = await handler(call)
        self.assertEqual(result["status"], "dry_run")
        verify = await self.hass.admin_services["verify"][0](type("Call", (), {"data": {}})())
        self.assertEqual(verify["checked"], 0)
        test = await self.hass.admin_services["write_test_state"][0](
            type("Call", (), {"data": {}})()
        )
        self.assertEqual(test["written"], 1)

    async def test_service_before_entry_is_loaded(self):
        from homeassistant.exceptions import ServiceValidationError

        from tests.ha_stubs import ConfigEntryState

        self.env.entry.state = ConfigEntryState.NOT_LOADED
        async_register_services(self.hass)
        with self.assertRaises(ServiceValidationError) as ctx:
            await self.hass.admin_services["verify"][0](type("Call", (), {"data": {}})())
        self.assertEqual(ctx.exception.translation_key, "not_loaded")


def flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


class StaticFileTests(unittest.TestCase):
    def load(self, rel):
        return json.loads((PKG / rel).read_text(encoding="utf-8"))

    def test_strings_equal_english_translation(self):
        self.assertEqual(self.load("strings.json"), self.load("translations/en.json"))

    def test_german_has_the_same_keys_and_placeholders(self):
        en, de = flat(self.load("translations/en.json")), flat(self.load("translations/de.json"))
        self.assertEqual(set(en), set(de))
        for key in en:
            self.assertEqual(
                sorted(re.findall(r"\{(\w+)\}", en[key])),
                sorted(re.findall(r"\{(\w+)\}", de[key])),
                key,
            )
            self.assertTrue(de[key].strip(), key)

    def test_services_yaml_matches_strings(self):
        text = (PKG / "services.yaml").read_text()
        services = {}
        current = None
        for line in text.splitlines():
            if re.match(r"^\w+:", line):
                current = line.split(":")[0]
                services[current] = []
            elif current and re.match(r"^    \w+:", line):
                services[current].append(line.strip().rstrip(":"))
        strings = self.load("strings.json")["services"]
        self.assertEqual(set(services), set(strings))
        for name, fields in services.items():
            self.assertEqual(set(fields), set(strings[name].get("fields", {})), name)
        icons = self.load("icons.json")["services"]
        self.assertEqual(set(icons), set(strings))

    def test_every_error_key_used_in_code_is_translated(self):
        strings = self.load("strings.json")
        exceptions = set(strings["exceptions"])
        used = set()
        for name in ("manager.py", "services.py"):
            used |= set(re.findall(r'ArchiveError\(\s*"(\w+)"', (PKG / name).read_text()))
            used |= set(re.findall(r'translation_key="(\w+)"', (PKG / name).read_text()))
        self.assertTrue(used)
        self.assertLessEqual(used, exceptions)
        # keys of WriteTargetError are shown as text in the import report; list them for review
        writer_keys = set(
            re.findall(r'WriteTargetError\(\s*"(\w+)"', (PKG / "recorder_writer.py").read_text())
        )
        writer_keys |= set(
            re.findall(r'WriteTargetError\(\s*"(\w+)"', (PKG / "entities.py").read_text())
        )
        self.assertTrue(writer_keys)

    def test_issue_texts_exist_for_every_finding_with_matching_placeholders(self):
        issues = self.load("strings.json")["issues"]
        self.assertEqual(set(issues), set(health.ALL_ISSUES))
        facts = health.HealthFacts(
            keep_days=1, dialect="sqlite", free_bytes=1, recording=False, unrecorded=("sensor.x",)
        )
        for finding in health.evaluate(facts):
            needed = set(
                re.findall(
                    r"\{(\w+)\}", issues[finding.key]["title"] + issues[finding.key]["description"]
                )
            )
            self.assertEqual(needed, set(finding.placeholders), finding.key)
        self.assertIn("entity_globs", issues["not_recorded"]["description"])
        self.assertIn("sensor.activity_archive_*", issues["not_recorded"]["description"])
        self.assertIn("device_tracker.activity_archive_*", issues["not_recorded"]["description"])
        self.assertEqual(
            set(re.findall(r"\{(\w+)\}", issues["ledger_unreadable"]["description"])), {"version"}
        )

    def test_config_flow_error_keys_are_translated(self):
        strings = self.load("strings.json")
        source = (PKG / "config_flow.py").read_text()
        keys = set(re.findall(r'ERR_\w+ = "(\w+)"', source)) | {
            "invalid_slug",
            "slug_reserved",
            "slug_exists",
        }
        self.assertLessEqual(keys - {"last_person"}, set(strings["config"]["error"]))
        self.assertLessEqual(keys, set(strings["options"]["error"]))

    def test_manifest(self):
        manifest = self.load("manifest.json")
        self.assertEqual(manifest["domain"], "activity_archive")
        self.assertTrue(manifest["config_flow"])
        self.assertIn("recorder", manifest["dependencies"])
        self.assertEqual(manifest["integration_type"], "service")
        self.assertIn(manifest["iot_class"], {"local_polling", "local_push", "calculated"})
        self.assertEqual(manifest["requirements"], [])
        for key in ("documentation", "issue_tracker", "codeowners", "version", "name"):
            self.assertTrue(manifest[key], key)
        self.assertEqual(list(manifest)[:2], ["domain", "name"])
        self.assertEqual(
            list(manifest)[2:], sorted(list(manifest)[2:]), "hassfest wants sorted keys"
        )
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn(f'version = "{manifest["version"]}"', pyproject)

    def test_hacs_json(self):
        hacs = json.loads((ROOT / "hacs.json").read_text())
        self.assertEqual(hacs["name"], "Activity Archive")
        self.assertRegex(hacs["homeassistant"], r"^\d{4}\.\d+\.\d+$")
        self.assertLessEqual(
            set(hacs),
            {
                "name",
                "homeassistant",
                "render_readme",
                "zip_release",
                "filename",
                "content_in_root",
            },
        )

    def test_manager_raises_archive_error_class(self):
        self.assertTrue(issubclass(ArchiveError, Exception))


if __name__ == "__main__":
    unittest.main()
