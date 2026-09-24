# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — la ville de l'utilisateur, et le critère qui prime sur la forme.

Trois pannes du 24/09, même racine pour deux d'entre elles : l'appli a été
écrite pour quelqu'un en France et le front n'a jamais demandé au serveur où
habite l'utilisateur.

  - L'horloge affichait « PARIS · HEURE LOCALE » au-dessus de l'heure de
    Montréal (fuseau Europe/Paris en dur).
  - La vue météo ouvrait sur Paris malgré HOME_CITY=Montreal.
  - `get_weather` exigeait une ville : le modèle en inventait une (« Reykjavik »
    recopié d'un exemple, puis « ville » recopié de la description de l'outil).

Et, sans rapport : proj_b2ca0d, étape 2 « écrire la date » a échoué alors que
le fichier contenait la date — l'étape 1 l'avait déjà écrite, l'étape 2 a
réécrit la même chose, aucun fichier modifié, et la couche structurelle a
recalé l'étape. Le critère de succès doit primer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from jarvis.engine.mission.schemas import Step
from jarvis.engine.mission.verifier import Verifier
from main import app

_STATIC = Path(__file__).resolve().parents[1] / "src/jarvis/interfaces/ui/static"


def _read(rel: str) -> str:
    return (_STATIC / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


# ── Le critère prime sur « un fichier a-t-il bougé » ────────────────────────


@pytest.mark.asyncio
async def test_met_criterion_is_not_overruled_by_the_llm(tmp_path: Path) -> None:
    """proj_b2ca0d : la date était déjà dans le fichier, l'étape a échoué.

    La couche structurelle passait (rien de malformé) et le `grep` du critère
    passait aussi — c'est le JUGE LLM, en couche 3, qui a recalé l'étape avec
    « Aucun fichier nouveau ou modifié ». Un critère objectivement atteint ne
    se discute pas : la couche 3 ne doit même pas être appelée.
    """
    (tmp_path / "bonjour.txt").write_text("2026-09-23\n", encoding="utf-8")
    quality = MagicMock()
    quality.check_step_output.return_value = []
    llm = MagicMock()
    llm.complete = MagicMock(side_effect=AssertionError("le LLM ne doit pas être consulté"))
    verifier = Verifier(
        quality_checker=quality, llm=llm, cli_executor=None, workspace_path=str(tmp_path)
    )
    step = Step(
        id="s2",
        title="Écrire la date",
        description="d",
        verification_command=r"grep -E '^\d{4}-\d{2}-\d{2}$' bonjour.txt",
    )

    result = await verifier.verify(MagicMock(), step, files_before=["bonjour.txt"])

    assert result.verified and result.layer == "deterministic"


@pytest.mark.asyncio
async def test_unmet_criterion_still_fails(tmp_path: Path) -> None:
    (tmp_path / "bonjour.txt").write_text("$(date +%Y-%m-%d)", encoding="utf-8")
    quality = MagicMock()
    quality.check_step_output.return_value = []
    verifier = Verifier(
        quality_checker=quality, llm=MagicMock(), cli_executor=None, workspace_path=str(tmp_path)
    )
    step = Step(
        id="s2",
        title="t",
        description="d",
        verification_command=r"grep -E '^\d{4}-\d{2}-\d{2}$' bonjour.txt",
    )

    result = await verifier.verify(MagicMock(), step, files_before=[])

    assert not result.verified


@pytest.mark.asyncio
async def test_structural_problem_still_caught_without_a_criterion(tmp_path: Path) -> None:
    """Non-régression : sans commande, la couche structurelle garde son rôle."""
    quality = MagicMock()
    quality.check_step_output.return_value = ["Fichier vide"]
    verifier = Verifier(
        quality_checker=quality, llm=MagicMock(), cli_executor=None, workspace_path=str(tmp_path)
    )

    result = await verifier.verify(MagicMock(), Step(id="s", title="t", description="d"), [])

    assert not result.verified and result.layer == "structural"


# ── L'endpoint de localisation ──────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_locale_endpoint_returns_the_configured_city(client: TestClient) -> None:
    body = client.get("/api/ui/locale").json()

    assert body["city"], "la ville configurée doit être exposée à l'interface"
    assert isinstance(body["lat"], (float, int)) and isinstance(body["lon"], (float, int))


def test_locale_resolves_a_city_written_without_accents() -> None:
    """HOME_CITY=Montreal doit trouver « montréal » dans la table."""
    from jarvis.interfaces.api.system import _city_coords

    assert _city_coords("Montreal") == pytest.approx((45.5017, -73.5673))
    assert _city_coords("montréal") == _city_coords("Montreal")
    assert _city_coords("Ville-Qui-N-Existe-Pas") is None


# ── get_weather sans ville ──────────────────────────────────────────────────


def test_weather_tool_no_longer_requires_a_city() -> None:
    from jarvis.capabilities.tools.weather import WeatherTool

    assert WeatherTool.input_schema["required"] == []


@pytest.mark.asyncio
async def test_weather_tool_falls_back_to_home_city(monkeypatch) -> None:
    from jarvis.capabilities.tools import weather as weather_mod

    called: dict[str, str] = {}

    class _Resp:
        text = "Montreal: ☀️ +14°C"

        def raise_for_status(self) -> None: ...

    class _Client:
        def __init__(self, *a, **k) -> None: ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a) -> None: ...
        async def get(self, url, *a, **k):
            called["url"] = url
            return _Resp()

    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(weather_mod.settings, "home_city", "Montreal")

    result = await weather_mod.WeatherTool().execute()

    assert not result.is_error
    assert "Montreal" in called["url"], "sans ville, HOME_CITY doit être utilisée"


# ── Plus de Paris en dur dans l'interface ───────────────────────────────────


def test_clock_uses_the_browser_timezone_and_the_configured_city() -> None:
    js = _read("skills/clock/view.js")

    assert "Europe/Paris" not in js, "le fuseau ne doit plus être figé sur la France"
    assert "resolvedOptions().timeZone" in js
    assert "/api/ui/locale" in js


def test_weather_view_defaults_to_the_configured_city() -> None:
    js = _read("skills/weather/view.js")

    assert "/api/ui/locale" in js
    assert "name: 'Paris'" not in js, "le repli ne doit plus être Paris"


def test_topbar_shows_the_configured_city() -> None:
    js = _read("_shared.js")

    assert 'text: "Paris"' not in js
    assert "j-topbar-city" in js and "/api/ui/locale" in js
