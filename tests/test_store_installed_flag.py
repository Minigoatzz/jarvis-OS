# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — badge « Installé » du store.

Bug réel : la vue globe était installée et chargée (elle figure bien dans
`/api/skills/installed`, type `view`) mais le store ne la marquait jamais comme
installée. Les trois autres vues — clock, system-monitor, weather — l'étaient.

Cause : deux espaces de noms qu'aucun invariant ne relie.
  - index.json (le store) publie la vue sous `name: "globe"`, `path:
    "views/globe"` ;
  - le manifeste posé sur disque déclare `name: globe-view`.
`item["name"] in {noms du registre}` échouait donc pour ce seul item. Les trois
autres passaient par coïncidence : leur `name` de catalogue et leur `name` de
manifeste sont identiques.

Le dossier d'installation (`skills_data/installed/globe/`) est le seul
identifiant partagé de bout en bout — c'est le critère ajouté.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.capabilities.skills import installer as mod


def _catalog() -> list[dict]:
    """Extrait réel de l'index publié (raw.githubusercontent, branche main)."""
    return [
        {"name": "clock", "path": "views/clock", "type": "view"},
        {"name": "globe", "path": "views/globe", "type": "view"},
        {"name": "system-monitor", "path": "views/system-monitor", "type": "view"},
        {"name": "weather", "path": "views/weather", "type": "view"},
        {"name": "youtube-analyzer", "path": "skills/youtube-analyzer", "type": "conversational"},
    ]


def _flag(catalog: list[dict], registry_names: set[str], dirs: set[str]) -> dict[str, bool]:
    """Rejoue la logique de marquage de fetch_catalog sans toucher au réseau."""
    installed_names = registry_names
    installed_dirs = dirs
    out: dict[str, bool] = {}
    for item in catalog:
        folder = str(item.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
        out[item["name"]] = item["name"] in installed_names or (
            bool(folder) and folder in installed_dirs
        )
    return out


# État réel de la machine au moment du bug.
_REGISTRY = {"clock", "globe-view", "system-monitor", "weather"}
_DIRS = {"clock", "globe", "system-monitor", "weather"}


def test_globe_is_marked_installed_despite_the_name_mismatch() -> None:
    """La régression exacte rapportée : « globe est installé mais pas marqué »."""
    flags = _flag(_catalog(), _REGISTRY, _DIRS)

    assert flags["globe"] is True


def test_the_three_matching_views_still_pass() -> None:
    """Non-régression : celles qui marchaient par coïncidence doivent tenir."""
    flags = _flag(_catalog(), _REGISTRY, _DIRS)

    assert flags["clock"] and flags["system-monitor"] and flags["weather"]


def test_a_non_installed_item_stays_unmarked() -> None:
    """Garde-fou : le second critère ne doit pas tout marquer comme installé."""
    flags = _flag(_catalog(), _REGISTRY, _DIRS)

    assert flags["youtube-analyzer"] is False


def test_an_item_without_path_falls_back_to_the_name() -> None:
    """Une entrée sans `path` ne doit ni planter ni être marquée à tort."""
    catalog = [{"name": "sans-chemin", "type": "view"}, {"name": "clock", "type": "view"}]

    flags = _flag(catalog, _REGISTRY, _DIRS)

    assert flags["sans-chemin"] is False
    assert flags["clock"] is True


# ── Le helper lui-même ──────────────────────────────────────────────────────


def test_installed_dir_names_lists_folders(tmp_path: Path) -> None:
    for name in ("globe", "clock"):
        (tmp_path / name).mkdir()
    (tmp_path / "un-fichier.txt").write_text("x", encoding="utf-8")

    with patch.object(mod, "SKILLS_INSTALLED_DIR", tmp_path):
        assert mod._installed_dir_names() == {"globe", "clock"}


def test_installed_dir_names_survives_a_missing_directory(tmp_path: Path) -> None:
    """Premier lancement : le dossier n'existe pas encore, le store doit tenir."""
    with patch.object(mod, "SKILLS_INSTALLED_DIR", tmp_path / "jamais-cree"):
        assert mod._installed_dir_names() == set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
