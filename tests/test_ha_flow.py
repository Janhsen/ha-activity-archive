"""Config flow and options flow against the fakes (marker: SYNTHETIC)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from custom_components.activity_archive.config_flow import (
    ActivityArchiveConfigFlow,
    ActivityArchiveOptionsFlow,
)
from custom_components.activity_archive.settings import parse_settings
from tests import ha_stubs


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = str(Path(self.tmp.name).resolve())
        self.hass = ha_stubs.FakeHass(self.config_dir)
        self.flow = ActivityArchiveConfigFlow()
        self.flow.hass = self.hass
        self.good = f"{self.config_dir}/activity_archive/import"

    def tearDown(self):
        self.tmp.cleanup()

    async def test_form_then_person_then_entry(self):
        first = await self.flow.async_step_user()
        self.assertEqual((first["type"], first["step_id"]), ("form", "user"))
        keys = {m.schema: m.default for m in first["data_schema"].schema}
        self.assertEqual(keys["import_dir"], "/config/activity_archive/import")
        self.assertEqual(keys["profile"], "precise")
        nxt = await self.flow.async_step_user({"import_dir": self.good, "profile": "standard"})
        self.assertEqual(nxt["step_id"], "person")
        done = await self.flow.async_step_person(
            {"name": "Alex Example", "slug": "", "athlete_name": " Alex E. "}
        )
        self.assertEqual(done["type"], "create_entry")
        settings = parse_settings(done["data"])
        self.assertEqual((settings.import_dir, settings.profile), (self.good, "standard"))
        self.assertEqual(
            [(p.slug, p.name, p.athlete_name) for p in settings.people],
            [("alex_example", "Alex Example", "Alex E.")],
        )

    async def test_explicit_slug_is_kept(self):
        await self.flow.async_step_user({"import_dir": self.good, "profile": "precise"})
        done = await self.flow.async_step_person(
            {"name": "Alex", "slug": "alex", "athlete_name": ""}
        )
        self.assertEqual(parse_settings(done["data"]).people[0].slug, "alex")

    async def test_folder_errors(self):
        rel = await self.flow.async_step_user({"import_dir": "import", "profile": "precise"})
        self.assertEqual(rel["errors"], {"import_dir": "path_not_absolute"})
        out = await self.flow.async_step_user({"import_dir": "/etc", "profile": "precise"})
        self.assertEqual(out["errors"], {"import_dir": "path_not_allowed"})
        trick = await self.flow.async_step_user(
            {"import_dir": f"{self.config_dir}/../etc", "profile": "precise"}
        )
        self.assertEqual(trick["errors"], {"import_dir": "path_not_allowed"})
        self.hass.config.allowed.append("/srv/archive")
        ok = await self.flow.async_step_user(
            {"import_dir": "/srv/archive/in", "profile": "precise"}
        )
        self.assertEqual(ok["step_id"], "person")

    async def test_person_errors(self):
        await self.flow.async_step_user({"import_dir": self.good, "profile": "precise"})
        for data, key in (
            ({"name": "  ", "slug": "", "athlete_name": ""}, "name_required"),
            ({"name": "Alex", "slug": "Alex Z", "athlete_name": ""}, "invalid_slug"),
            ({"name": "Heart", "slug": "heart_rate", "athlete_name": ""}, "slug_reserved"),
            ({"name": "!!!", "slug": "", "athlete_name": ""}, "invalid_slug"),
        ):
            res = await self.flow.async_step_person(data)
            self.assertEqual(res["errors"], {"base": key}, data)
            self.assertEqual(res["type"], "form")

    async def test_only_one_instance_unique_id(self):
        await self.flow.async_step_user()
        self.assertEqual(self.flow.unique_id, "activity_archive")


class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = str(Path(self.tmp.name).resolve())
        self.hass = ha_stubs.FakeHass(self.config_dir)
        data = {
            "import_dir": f"{self.config_dir}/in",
            "profile": "precise",
            "people": [{"name": "Alex", "slug": "alex", "athlete_name": "A"}],
        }
        self.entry = ha_stubs.ConfigEntry(data)
        self.flow = ActivityArchiveOptionsFlow()
        self.flow.hass, self.flow.config_entry = self.hass, self.entry

    def tearDown(self):
        self.tmp.cleanup()

    async def test_menu_depends_on_number_of_people(self):
        self.assertEqual(
            (await self.flow.async_step_init())["menu_options"],
            ["settings", "add_person", "edit_person"],
        )
        await self.flow.async_step_add_person({"name": "Sam", "slug": "sam", "athlete_name": ""})
        self.assertIn("remove_person", (await self.flow.async_step_init())["menu_options"])

    async def test_change_settings_keeps_people(self):
        res = await self.flow.async_step_settings(
            {"import_dir": f"{self.config_dir}/other", "profile": "coarse"}
        )
        s = parse_settings(self.entry.data, res["data"])
        self.assertEqual((s.profile, s.import_dir.endswith("/other")), ("coarse", True))
        self.assertEqual([p.slug for p in s.people], ["alex"])
        bad = await self.flow.async_step_settings({"import_dir": "/etc", "profile": "coarse"})
        self.assertEqual(bad["errors"], {"import_dir": "path_not_allowed"})

    async def test_add_person_and_duplicate(self):
        res = await self.flow.async_step_add_person({"name": "Sam", "slug": "", "athlete_name": ""})
        self.assertEqual([p.slug for p in parse_settings({}, res["data"]).people], ["alex", "sam"])
        dup = await self.flow.async_step_add_person(
            {"name": "Alex 2", "slug": "alex", "athlete_name": ""}
        )
        self.assertEqual(dup["errors"], {"base": "slug_exists"})

    async def test_edit_person_cannot_change_slug(self):
        picker = await self.flow.async_step_edit_person()
        self.assertEqual(picker["step_id"], "edit_person")
        form = await self.flow.async_step_edit_person({"person": "alex"})
        self.assertEqual(form["step_id"], "edit_person_details")
        self.assertNotIn("slug", {m.schema for m in form["data_schema"].schema})
        defaults = {m.schema: m.default for m in form["data_schema"].schema}
        self.assertEqual(defaults, {"name": "Alex", "athlete_name": "A"})
        res = await self.flow.async_step_edit_person_details(
            {"name": "Alexandra", "athlete_name": ""}
        )
        person = parse_settings({}, res["data"]).people[0]
        self.assertEqual((person.slug, person.name, person.athlete_name), ("alex", "Alexandra", ""))
        empty = await self.flow.async_step_edit_person_details({"name": " ", "athlete_name": ""})
        self.assertEqual(empty["errors"], {"base": "name_required"})

    async def test_remove_person_keeps_at_least_one(self):
        await self.flow.async_step_add_person({"name": "Sam", "slug": "sam", "athlete_name": ""})
        res = await self.flow.async_step_remove_person({"person": "sam"})
        self.assertEqual([p.slug for p in parse_settings({}, res["data"]).people], ["alex"])
        last = await self.flow.async_step_remove_person({"person": "alex"})
        self.assertEqual(last["errors"], {"base": "last_person"})


if __name__ == "__main__":
    unittest.main()
