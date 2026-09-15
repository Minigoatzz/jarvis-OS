# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — backoff du rappel calendrier (JRV-BG-001) et sévérité credentials (JRV-TOL-015).

Reproduit un bug observé en usage réel : Scheduler._calendar_loop retentait
CalendarListTool.execute() toutes les 60s indéfiniment, sans repli, même quand
l'échec est permanent (credentials Google jamais configurés). Preuve tirée des
logs réels : 844 occurrences de "Credentials Google manquants" en ~12h, chacune
loggée en ERROR. Deux correctifs couverts ici :

1. CalendarListTool.execute() : un FileNotFoundError (pas configuré) émet
   désormais JRV-TOL-015, enregistré en `warning`, au lieu de JRV-TOL-001,
   enregistré en `error`. Changer le niveau au site d'appel ne suffisait PAS :
   ErrorCollector.emit() résout la sévérité depuis ERROR_REGISTRY avant de
   regarder le niveau de l'appelant. Les vraies pannes restent en ERROR.
2. Scheduler._calendar_loop : backoff exponentiel plafonné (60s → 1800s) sur
   échecs consécutifs, réinitialisé au premier succès qui suit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Never
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from jarvis.capabilities.tools.base import ToolResult
    from jarvis.engine.background.scheduler import Scheduler

# ── CalendarListTool : sévérité credentials (JRV-TOL-015) ──────────────────────
#
# Ces tests capturent le niveau RÉELLEMENT émis par loguru, via le vrai
# ErrorCollector — ils ne mockent pas `collector`. C'est la seule formulation
# qui a du sens ici : une première version de ces tests mockait le collector et
# vérifiait que `.warning()` était appelé, ce qui est tautologique (ça teste que
# le code appelle ce qu'on vient d'écrire). Elle passait au vert alors que la
# ligne ERROR continuait de sortir en production, parce que
# ErrorCollector.emit() résout la sévérité depuis ERROR_REGISTRY *avant* le
# niveau de l'appelant : `severity = spec.get("severity") or _LEVEL_TO_...`.
# Un code enregistré en `error` ignore donc collector.warning(). D'où
# JRV-TOL-015, enregistré en `warning`.


@pytest.fixture
def loguru_levels() -> Iterator[list[str]]:
    """Capture le niveau de chaque enregistrement loguru émis pendant le test."""
    from loguru import logger

    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(m.record["level"].name), level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def test_jrv_tol_015_is_registered_as_warning() -> None:
    """Garde-fou : c'est le registre, pas le site d'appel, qui fixe la sévérité.

    Si quelqu'un repasse ce code en `error` dans error-codes.yaml, le correctif
    calendrier redevient silencieusement inopérant — ce test le signale.
    """
    from jarvis.kernel._error_codes_generated import ERROR_REGISTRY

    assert ERROR_REGISTRY["JRV-TOL-015"]["severity"] == "warning"
    assert ERROR_REGISTRY["JRV-TOL-001"]["severity"] == "error"


@pytest.mark.asyncio
async def test_calendar_missing_credentials_emits_warning_level(
    tmp_path: Path, loguru_levels: list[str]
) -> None:
    """Pas configuré -> loguru émet WARNING (et zéro ERROR), collector réel."""
    from jarvis.capabilities.tools.calendar import CalendarListTool

    tool = CalendarListTool(
        credentials_path=tmp_path / "missing_creds.json", token_path=tmp_path / "token.json"
    )

    with patch(
        "jarvis.capabilities.tools.calendar._load_creds",
        side_effect=FileNotFoundError("Credentials Google manquants : missing_creds.json"),
    ):
        result = await tool.execute(days_ahead=2)

    assert result.is_error
    assert "Erreur credentials" in result.content
    assert "WARNING" in loguru_levels
    assert "ERROR" not in loguru_levels, f"niveau ERROR encore émis : {loguru_levels}"


@pytest.mark.asyncio
async def test_calendar_other_exception_still_emits_error_level(
    tmp_path: Path, loguru_levels: list[str]
) -> None:
    """Une panne réelle reste en ERROR — le correctif ne doit rien masquer d'autre."""
    from jarvis.capabilities.tools.calendar import CalendarListTool

    tool = CalendarListTool(
        credentials_path=tmp_path / "creds.json", token_path=tmp_path / "token.json"
    )

    with patch(
        "jarvis.capabilities.tools.calendar._load_creds",
        side_effect=RuntimeError("token corrompu"),
    ):
        result = await tool.execute(days_ahead=2)

    assert result.is_error
    assert "ERROR" in loguru_levels


@pytest.mark.asyncio
async def test_calendar_missing_credentials_stderr_line_says_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """La ligne stderr `[JRV-...] WARN:` et la ligne loguru doivent s'accorder.

    Avant le correctif elles se contredisaient : `[JRV-TOL-001] WARN:` suivi de
    `| ERROR |` pour le même événement (emit_to_stderr utilise le niveau de
    l'appelant, loguru celui du registre).
    """
    from jarvis.capabilities.tools.calendar import CalendarListTool

    tool = CalendarListTool(
        credentials_path=tmp_path / "missing_creds.json", token_path=tmp_path / "token.json"
    )

    with patch(
        "jarvis.capabilities.tools.calendar._load_creds",
        side_effect=FileNotFoundError("Credentials Google manquants : missing_creds.json"),
    ):
        await tool.execute(days_ahead=2)

    err = capsys.readouterr().err
    assert "[JRV-TOL-015] WARN:" in err
    assert "[JRV-TOL-015] ERROR:" not in err


@pytest.mark.asyncio
async def test_calendar_success_unaffected(tmp_path: Path) -> None:
    """Chemin heureux inchangé : événements listés normalement."""
    from jarvis.capabilities.tools.calendar import CalendarListTool

    tool = CalendarListTool(
        credentials_path=tmp_path / "creds.json", token_path=tmp_path / "token.json"
    )

    fake_creds = MagicMock(token="fake-token")  # noqa: S106

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "items": [{"start": {"dateTime": "2026-09-15T10:00:00+00:00"}, "summary": "Réunion"}]
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch(
        "jarvis.capabilities.tools.calendar._load_creds", return_value=fake_creds
    ), patch("jarvis.capabilities.tools.calendar.httpx.AsyncClient", return_value=mock_client):
        result = await tool.execute(days_ahead=2)

    assert not result.is_error
    assert "Réunion" in result.content


# ── Scheduler._check_reminders : valeur de retour (True/False) ────────────────


class _FakeCalendarTool:
    def __init__(self, results: list) -> None:
        self._results = results
        self.calls = 0

    async def execute(self, days_ahead: int = 2, **_: object) -> ToolResult:
        idx = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[idx]


def _make_scheduler(calendar_tool: object) -> Scheduler:
    from jarvis.engine.background.notifications import ProactiveQueue
    from jarvis.engine.background.scheduler import Scheduler
    from jarvis.kernel.settings import settings

    return Scheduler(
        proactive=ProactiveQueue(),
        auto_dream=None,  # type: ignore[arg-type]  # non utilisé par _check_reminders/_calendar_loop
        calendar_tool=calendar_tool,  # type: ignore[arg-type]
        settings=settings,
    )


@pytest.mark.asyncio
async def test_check_reminders_returns_false_on_tool_error() -> None:
    from jarvis.capabilities.tools.base import ToolResult

    tool = _FakeCalendarTool([ToolResult(content="erreur", is_error=True)])
    scheduler = _make_scheduler(tool)

    ok = await scheduler._check_reminders(set())
    assert ok is False


@pytest.mark.asyncio
async def test_check_reminders_returns_false_on_exception() -> None:
    class _RaisingTool:
        async def execute(self, days_ahead: int = 2, **_: object) -> Never:
            raise RuntimeError("boom")

    scheduler = _make_scheduler(_RaisingTool())
    ok = await scheduler._check_reminders(set())
    assert ok is False


@pytest.mark.asyncio
async def test_check_reminders_returns_true_on_success_even_with_no_events() -> None:
    from jarvis.capabilities.tools.base import ToolResult

    tool = _FakeCalendarTool([ToolResult(content="Aucun événement prévu.")])
    scheduler = _make_scheduler(tool)

    ok = await scheduler._check_reminders(set())
    assert ok is True


# ── Scheduler._calendar_loop : backoff exponentiel plafonné ────────────────────


@pytest.mark.asyncio
async def test_calendar_loop_backoff_grows_and_caps_on_persistent_failure() -> None:
    """Échecs consécutifs -> 60, 120, 240, 480, 960, 1800 (plafond), 1800, ..."""
    from jarvis.capabilities.tools.base import ToolResult

    tool = _FakeCalendarTool([ToolResult(content="pas configuré", is_error=True)])
    scheduler = _make_scheduler(tool)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 7:
            raise asyncio.CancelledError()

    with patch("jarvis.engine.background.scheduler.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await scheduler._calendar_loop()

    # sleep_calls[0] = délai initial (10s), puis backoff après chaque échec :
    # 60*2=120, 60*4=240, 60*8=480, 60*16=960, 60*32=1920->plafonné à 1800, puis 1800.
    assert sleep_calls == [10, 120, 240, 480, 960, 1800, 1800]


@pytest.mark.asyncio
async def test_calendar_loop_backoff_resets_after_success() -> None:
    """Un succès après des échecs doit réinitialiser le délai à 60s."""
    from jarvis.capabilities.tools.base import ToolResult

    results = [
        ToolResult(content="pas configuré", is_error=True),  # échec 1 -> 120s
        ToolResult(content="pas configuré", is_error=True),  # échec 2 -> 240s
        ToolResult(content="Aucun événement prévu."),  # succès -> reset, 60s
        ToolResult(content="pas configuré", is_error=True),  # échec 1 à nouveau -> 120s
    ]
    tool = _FakeCalendarTool(results)
    scheduler = _make_scheduler(tool)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 5:
            raise asyncio.CancelledError()

    with patch("jarvis.engine.background.scheduler.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await scheduler._calendar_loop()

    # [10 (initial), 120 (échec 1), 240 (échec 2), 60 (succès -> reset), 120 (échec 1)]
    assert sleep_calls == [10, 120, 240, 60, 120]
