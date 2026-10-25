"""Config flow and options flow: people, import folder and profile."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_IMPORT_DIR,
    CONF_PROFILE,
    DEFAULT_IMPORT_DIR,
    DEFAULT_PROFILE,
    DOMAIN,
    PROFILES,
)
from .core.people import Person, slugify, validate_slug
from .settings import Settings, parse_settings

CONF_NAME = "name"
CONF_SLUG = "slug"
CONF_ATHLETE = "athlete_name"
CONF_PERSON = "person"

ERR_PATH_RELATIVE = "path_not_absolute"
ERR_PATH_NOT_ALLOWED = "path_not_allowed"
ERR_NAME_REQUIRED = "name_required"
ERR_LAST_PERSON = "last_person"


def _check_dir(import_dir: str, config_dir: str, is_allowed: Any) -> str | None:
    """Blocking: return an error key if the folder may not be used."""
    path = Path(import_dir)
    if not path.is_absolute():
        return ERR_PATH_RELATIVE
    resolved = path.resolve()
    if resolved.is_relative_to(Path(config_dir).resolve()) or is_allowed(str(resolved)):
        return None
    return ERR_PATH_NOT_ALLOWED


async def _folder_error(hass: HomeAssistant, import_dir: str) -> str | None:
    return await hass.async_add_executor_job(
        _check_dir, import_dir, hass.config.config_dir, hass.config.is_allowed_path
    )


def _profile_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=list(PROFILES), mode=SelectSelectorMode.LIST, translation_key="profile"
        )
    )


def _settings_schema(import_dir: str, profile: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_IMPORT_DIR, default=import_dir): TextSelector(),
            vol.Required(CONF_PROFILE, default=profile): _profile_selector(),
        }
    )


def _person_schema(
    defaults: Mapping[str, Any] | None = None, *, with_slug: bool = True
) -> vol.Schema:
    d = defaults or {}
    fields: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): TextSelector(),
    }
    if with_slug:
        fields[vol.Optional(CONF_SLUG, default=d.get(CONF_SLUG, ""))] = TextSelector()
    fields[vol.Optional(CONF_ATHLETE, default=d.get(CONF_ATHLETE, ""))] = TextSelector()
    return vol.Schema(fields)


def _make_person(
    user_input: Mapping[str, Any], existing: list[str]
) -> tuple[Person | None, str | None]:
    """Build a person from form input or return an error key."""
    name = str(user_input.get(CONF_NAME, "")).strip()
    if not name:
        return None, ERR_NAME_REQUIRED
    slug = str(user_input.get(CONF_SLUG) or "").strip() or slugify(name)
    error = validate_slug(slug, existing)
    if error:
        return None, error
    return Person(slug, name, str(user_input.get(CONF_ATHLETE) or "").strip()), None


class ActivityArchiveConfigFlow(ConfigFlow, domain=DOMAIN):
    """First set-up: import folder, profile and the first person."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Remember the folder and profile between the two steps."""
        self._import_dir = DEFAULT_IMPORT_DIR
        self._profile = DEFAULT_PROFILE

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the import folder and the profile."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._import_dir = str(user_input[CONF_IMPORT_DIR]).strip()
            self._profile = user_input[CONF_PROFILE]
            error = await _folder_error(self.hass, self._import_dir)
            if error is None:
                return await self.async_step_person()
            errors[CONF_IMPORT_DIR] = error
        return self.async_show_form(
            step_id="user",
            data_schema=_settings_schema(self._import_dir, self._profile),
            errors=errors,
        )

    async def async_step_person(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the first person and create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            person, error = _make_person(user_input, [])
            if person is not None:
                settings = Settings(self._import_dir, self._profile, (person,))
                return self.async_create_entry(title="Activity Archive", data=settings.to_data())
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="person", data_schema=_person_schema(user_input), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return ActivityArchiveOptionsFlow()


class ActivityArchiveOptionsFlow(OptionsFlow):
    """Change folder and profile, add, edit or remove people."""

    def __init__(self) -> None:
        """Work on a copy; the entry is only updated at the end of a step."""
        self._settings: Settings | None = None
        self._editing: str | None = None

    @property
    def settings(self) -> Settings:
        """Settings as they are stored in the entry (options win over data)."""
        if self._settings is None:
            self._settings = parse_settings(self.config_entry.data, self.config_entry.options)
        return self._settings

    def _save(self, settings: Settings) -> ConfigFlowResult:
        self._settings = settings
        return self.async_create_entry(title="", data=settings.to_data())

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the menu."""
        options = ["settings", "add_person"]
        if self.settings.people:
            options.append("edit_person")
        if len(self.settings.people) > 1:
            options.append("remove_person")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change import folder and profile."""
        errors: dict[str, str] = {}
        s = self.settings
        if user_input is not None:
            import_dir = str(user_input[CONF_IMPORT_DIR]).strip()
            error = await _folder_error(self.hass, import_dir)
            if error is None:
                return self._save(Settings(import_dir, user_input[CONF_PROFILE], s.people))
            errors[CONF_IMPORT_DIR] = error
        return self.async_show_form(
            step_id="settings", data_schema=_settings_schema(s.import_dir, s.profile), errors=errors
        )

    async def async_step_add_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a person."""
        errors: dict[str, str] = {}
        s = self.settings
        if user_input is not None:
            person, error = _make_person(user_input, [p.slug for p in s.people])
            if person is not None:
                return self._save(Settings(s.import_dir, s.profile, (*s.people, person)))
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="add_person", data_schema=_person_schema(user_input), errors=errors
        )

    def _picker_schema(self) -> vol.Schema:
        options = [{"value": p.slug, "label": p.name} for p in self.settings.people]
        return vol.Schema(
            {
                vol.Required(CONF_PERSON): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )

    async def async_step_edit_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the person to edit."""
        if user_input is None:
            return self.async_show_form(step_id="edit_person", data_schema=self._picker_schema())
        self._editing = user_input[CONF_PERSON]
        return await self.async_step_edit_person_details()

    async def async_step_edit_person_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change name and Strava athlete name. The slug (entity ids) can not be changed."""
        s = self.settings
        current = next((p for p in s.people if p.slug == self._editing), None)
        if current is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_NAME, "")).strip()
            if name:
                updated = Person(
                    current.slug, name, str(user_input.get(CONF_ATHLETE) or "").strip()
                )
                people = tuple(updated if p.slug == current.slug else p for p in s.people)
                return self._save(Settings(s.import_dir, s.profile, people))
            errors["base"] = ERR_NAME_REQUIRED
        defaults = {CONF_NAME: current.name, CONF_ATHLETE: current.athlete_name}
        return self.async_show_form(
            step_id="edit_person_details",
            data_schema=_person_schema(user_input or defaults, with_slug=False),
            errors=errors,
        )

    async def async_step_remove_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a person from the settings. Written states and ledger entries stay."""
        s = self.settings
        if user_input is None:
            return self.async_show_form(step_id="remove_person", data_schema=self._picker_schema())
        people = tuple(p for p in s.people if p.slug != user_input[CONF_PERSON])
        if not people:
            return self.async_show_form(
                step_id="remove_person",
                data_schema=self._picker_schema(),
                errors={"base": ERR_LAST_PERSON},
            )
        return self._save(Settings(s.import_dir, s.profile, people))
