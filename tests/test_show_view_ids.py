# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — `show_view` ne doit pas annoncer une vue qu'il n'a pas affichée.

Observé le 17/09 : « montre moi le cockpit » → « Voilà le cockpit. Tu peux
maintenant voir les informations système en temps réel. » et l'écran ne bouge
pas. Idem pour la météo.

Chaîne : le modèle nomme la vue comme l'utilisateur la nomme (« cockpit »,
« météo »), jamais par son identifiant technique. `ShowViewTool` diffusait le
`view_id` tel quel et rendait « Vue X affichée. » sans rien vérifier — côté
page, `Jarvis.views.activate(id)` fait `if (!view) return;` et sort en silence
quand l'id n'est pas enregistré. Un échec total, présenté comme un succès.

Les identifiants réels viennent des `VIEW_ID` déclarés dans chaque
`static/skills/<dir>/view.js` : clock, globe, system-monitor, weather.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jarvis.capabilities.tools.show_view import (
    ShowViewTool,
    _available_views,
    _resolve_view_id,
)

_REAL_VIEWS = ["clock", "globe", "system-monitor", "weather"]


def _tool() -> tuple[ShowViewTool, MagicMock]:
    broadcast = MagicMock()
    return ShowViewTool(broadcast_event=broadcast), broadcast


# ── Alias : le vocabulaire humain vers l'id technique ────────────────────────


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("cockpit", "system-monitor"),  # le cas exact observé
        ("météo", "weather"),
        ("meteo", "weather"),
        ("Système", "system-monitor"),
        ("horloge", "clock"),
        ("carte", "globe"),
        ("globe-view", "globe"),  # nom du manifeste ≠ id client
        ("weather", "weather"),  # un id déjà correct reste intact
    ],
)
def test_spoken_names_resolve_to_real_view_ids(spoken: str, expected: str) -> None:
    assert _resolve_view_id(spoken) == expected


# ── Validation : un id inconnu est une ERREUR, pas un succès ────────────────


@pytest.mark.asyncio
async def test_unknown_view_is_an_error_and_broadcasts_nothing() -> None:
    tool, broadcast = _tool()

    with patch(
        "jarvis.capabilities.tools.show_view._available_views", return_value=_REAL_VIEWS
    ):
        result = await tool.execute(action="show", view_id="dashboard-3d")

    assert result.is_error, "annoncer une vue inexistante est exactement le bug"
    assert broadcast.call_count == 0, "rien ne doit partir sur le bus"
    assert "system-monitor" in result.content, "l'erreur doit lister les vues réelles"


@pytest.mark.asyncio
async def test_cockpit_now_shows_the_system_monitor_view() -> None:
    """Le cas de l'utilisateur, de bout en bout."""
    tool, broadcast = _tool()

    with patch(
        "jarvis.capabilities.tools.show_view._available_views", return_value=_REAL_VIEWS
    ):
        result = await tool.execute(action="show", view_id="cockpit")

    assert not result.is_error
    broadcast.assert_called_once_with({"type": "show_view", "view_id": "system-monitor"})


@pytest.mark.asyncio
async def test_a_real_view_still_works() -> None:
    tool, broadcast = _tool()

    with patch(
        "jarvis.capabilities.tools.show_view._available_views", return_value=_REAL_VIEWS
    ):
        result = await tool.execute(action="show", view_id="globe")

    assert not result.is_error
    broadcast.assert_called_once_with({"type": "show_view", "view_id": "globe"})


@pytest.mark.asyncio
async def test_validation_is_skipped_when_nothing_can_be_listed() -> None:
    """Install partielle : mieux vaut laisser passer que tout bloquer."""
    tool, broadcast = _tool()

    with patch("jarvis.capabilities.tools.show_view._available_views", return_value=[]):
        result = await tool.execute(action="show", view_id="peu-importe")

    assert not result.is_error
    assert broadcast.call_count == 1


@pytest.mark.asyncio
async def test_hide_and_view_command_are_validated_too() -> None:
    tool, broadcast = _tool()

    with patch(
        "jarvis.capabilities.tools.show_view._available_views", return_value=_REAL_VIEWS
    ):
        hidden = await tool.execute(action="hide", view_id="inexistante")
        commanded = await tool.execute(
            action="view_command", view_id="inexistante", command="zoom_in"
        )

    assert hidden.is_error
    assert commanded.is_error
    assert broadcast.call_count == 0


# ── La détection sur disque ─────────────────────────────────────────────────


def test_available_views_matches_the_endpoint_the_page_actually_uses() -> None:
    """`_available_views` doit appliquer les deux mêmes conditions que
    /api/skills/view-scripts : un view.js servi ET un manifeste type=view.

    Sans la seconde, `astronomy` (présent en statique, jamais installé) serait
    proposé au modèle alors que la page ne le charge pas."""
    root = Path(__file__).resolve().parents[1]
    static_views = {
        d.name
        for d in (root / "src" / "jarvis" / "interfaces" / "ui" / "static" / "skills").iterdir()
        if d.is_dir() and (d / "view.js").exists()
    }

    detected = set(_available_views())

    assert detected <= static_views, "une vue sans view.js ne peut pas être activée"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
