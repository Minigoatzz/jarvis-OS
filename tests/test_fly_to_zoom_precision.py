# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — le zoom de `fly_to` suit la précision trouvée, pas un défaut fixe.

Observé le 21/09 : « montre moi le 655 rue des fauvettes » → la carte vole
vers le bon endroit mais s'arrête à la vue de toute la région de Montréal.
Cause : `zoom: int = 10` (vue « ville ») en dur, et le modèle ne passe
jamais de zoom. Nominatim renvoie pourtant la précision de chaque résultat
(`place_rank`) : on s'en sert.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis.capabilities.tools import show_view as sv
from jarvis.capabilities.tools.show_view import ShowViewTool, _precision_note, _zoom_for_rank


@pytest.mark.parametrize(
    ("rank", "zoom"),
    [(4, 4), (8, 6), (16, 11), (19, 13), (26, 16), (30, 17), (None, None)],
)
def test_zoom_follows_geocoder_precision(rank, zoom) -> None:
    assert _zoom_for_rank(rank) == zoom


def test_partial_address_is_reported_honestly() -> None:
    assert _precision_note("655 rue des Fauvettes", 30) == ""
    assert "Numéro introuvable" in _precision_note("655 rue des Fauvettes", 26)
    assert "secteur" in _precision_note("655 rue des Fauvettes", 16)
    assert _precision_note("Paris", 16) == "", "pas de numéro demandé, rien à signaler"
    assert _precision_note("655 rue X", None) == ""


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    """Remplace httpx.AsyncClient : renvoie une réponse Nominatim figée."""

    payload: list = []

    def __init__(self, *a, **k) -> None: ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a) -> None: ...

    async def get(self, *a, **k):
        return _Resp(_Client.payload)


async def _fly(location: str, payload: list, zoom=None):
    sent: list[dict] = []
    tool = ShowViewTool(broadcast_event=sent.append)
    _Client.payload = payload
    kwargs = {"action": "fly_to", "location": location}
    if zoom is not None:
        kwargs["zoom"] = zoom
    with patch.object(sv.httpx, "AsyncClient", _Client), patch.object(
        sv, "_available_views", return_value=["globe"]
    ):
        result = await tool.execute(**kwargs)
    fly = [e for e in sent if e.get("command") == "fly_to"]
    return result, (fly[0]["params"] if fly else None)


@pytest.mark.asyncio
async def test_exact_address_flies_at_street_level() -> None:
    """Le cas exact du 21/09."""
    result, params = await _fly(
        "655 rue des Fauvettes, Longueuil",
        [{"lat": "45.53", "lon": "-73.47", "place_rank": 30}],
    )
    assert not result.is_error
    assert params["zoom"] == 17, "une adresse exacte ne doit plus montrer toute la région"
    assert result.content == "Navigation vers 655 rue des Fauvettes, Longueuil."


@pytest.mark.asyncio
async def test_street_only_match_says_so() -> None:
    result, params = await _fly(
        "655 rue des Fauvettes", [{"lat": "45.53", "lon": "-73.47", "place_rank": 26}]
    )
    assert params["zoom"] == 16
    assert "Numéro introuvable" in result.content


@pytest.mark.asyncio
async def test_geocoder_precision_beats_the_models_guess() -> None:
    _result, params = await _fly("Longueuil", [{"lat": "45.53", "lon": "-73.51", "place_rank": 16}], zoom=3)
    assert params["zoom"] == 11


@pytest.mark.asyncio
async def test_missing_rank_keeps_the_previous_behaviour() -> None:
    _r, params = await _fly("Quelque part", [{"lat": "1", "lon": "2"}])
    assert params["zoom"] == 10
    _r, params = await _fly("Quelque part", [{"lat": "1", "lon": "2"}], zoom=14)
    assert params["zoom"] == 14


@pytest.mark.asyncio
async def test_local_table_hit_is_unchanged() -> None:
    """Paris vient de CITY_COORDS, sans rang : comportement d'avant."""
    _r, params = await _fly("paris", [])
    assert params["zoom"] == 10
