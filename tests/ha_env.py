"""Shared set-up for the HA-layer tests (marker: SYNTHETIC)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests import ha_stubs

ha_stubs.install()

from custom_components.activity_archive.core.people import Person  # noqa: E402
from custom_components.activity_archive.manager import ArchiveManager  # noqa: E402
from custom_components.activity_archive.settings import Settings  # noqa: E402
from tests.fixtures import generator as g  # noqa: E402

PEOPLE = (Person("alex", "Alex Example", "Alex E."), Person("sam", "Sam Example"))


def fit_bytes(n: int = 400, *, seed: int = 3, gps: bool = True, ascent: int = 321) -> bytes:
    samples = g.make_track(n, gps=gps, power=False, cadence=False, wander=True, seed=seed)
    session = g.session_for(samples, 2, 8, ascent=ascent, descent=300, avg_hr=131, max_hr=150)
    return g.encode_fit(samples, session=session, local_offset_s=7200, serial=99 + seed)


class Env:
    """A fake Home Assistant with a temporary configuration folder."""

    def __init__(self, people=PEOPLE, profile: str = "precise") -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name).resolve()
        self.import_dir = self.config_dir / "activity_archive" / "import"
        self.import_dir.mkdir(parents=True)
        self.hass = ha_stubs.FakeHass(str(self.config_dir))
        self.settings = Settings(str(self.import_dir), profile, tuple(people))
        self.entry = ha_stubs.ConfigEntry(self.settings.to_data())
        self.hass.config_entries.entries.append(self.entry)
        self.manager = self.new_manager()

    def new_manager(self) -> ArchiveManager:
        """A fresh manager on the same hass: simulates a restart of Home Assistant."""
        self.manager = ArchiveManager(self.hass, self.entry)
        self.entry.runtime_data = self.manager
        return self.manager

    def put_fit(self, name: str = "a.fit", **kw) -> Path:
        path = self.import_dir / name
        path.write_bytes(fit_bytes(**kw))
        return path

    def close(self) -> None:
        self.tmp.cleanup()

    @property
    def recorder(self):
        return self.hass.recorder
