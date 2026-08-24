"""Every setting the code reads must be documented in `.env.example`.

`.env` is gitignored, so `.env.example` is the only description of the
environment a newcomer — or the author six months later — gets. The failure
mode this guards against has already happened once: a field was added to a
settings class and the template was updated separately, by hand, after someone
noticed. This makes the code the source of truth and the template a checked
consequence of it.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

from adzuna_pipeline import config

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def _declared_aliases() -> dict[str, str]:
    """Map every env-var alias declared in config.py to its settings class."""
    aliases: dict[str, str] = {}
    for name, obj in inspect.getmembers(config, inspect.isclass):
        if not issubclass(obj, BaseSettings) or obj is BaseSettings:
            continue
        for field in obj.model_fields.values():
            if field.alias:
                aliases[field.alias] = name
    return aliases


def _documented_keys() -> set[str]:
    """Return every key named in `.env.example`, including commented-out ones.

    A line like `# ATHENA_WORKGROUP=primary` documents an optional setting just
    as well as an uncommented one, so both count.
    """
    keys: set[str] = set()
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("#").strip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.isupper() and key.replace("_", "").isalnum():
            keys.add(key)
    return keys


def test_config_declares_some_settings() -> None:
    """Guard the guard: a broken reflection would make every check vacuous."""
    assert len(_declared_aliases()) > 10


@pytest.mark.parametrize(
    ("alias", "owner"),
    sorted(_declared_aliases().items()),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_every_setting_is_documented(alias: str, owner: str) -> None:
    assert alias in _documented_keys(), f"{owner} reads {alias}, but .env.example never mentions it"
