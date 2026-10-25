"""Tests. The integration package imports Home Assistant in its ``__init__``, so the fakes in
``ha_stubs`` are installed first (they step aside when a real Home Assistant is importable)."""

from tests import ha_stubs

ha_stubs.install()
