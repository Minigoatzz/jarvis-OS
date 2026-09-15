# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — backoff du rappel calendrier (JRV-BG-001) et sévérité credentials (JRV-TOL-001).

Reproduit un bug observé en usage réel : Scheduler._calendar_loop retentait
CalendarListTool.execute() toutes les 60s indéfiniment, sans repli, même quand
l'échec est permanent (credentials Google jamais configurés). Preuve tirée des
logs réels : 844 occurrences de "Credentials Google manquants" en ~12h, chacune
loggée en ERROR. Deux correctifs couverts ici :

1. CalendarListTool.execute() : un FileNotFoundError (pas configuré) est
   maintenant loggé en WARNING, pas en ERROR — même traitement que
   EmailCollector pour le même cas (JRV-PRO-001). Les autres exceptions
   restent en ERROR.
2. Scheduler._calendar_loop : backoff exponentiel plafonné (60s → 1800s) sur
   échecs consécutifs, réinitialisé au premier succès qui suit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── CalendarListTool : sévérité credentials (JRV-TOL-001) ──────────────────────


@pytest.mark.asyncio
async def test_calendar_missing_credentials_logs_warning_not_error(tmp_path: Path) -> None:
    """FileNotFoundError (pas configuré) -> collector.warning, pas collector.error."""
    from jarvis.capabilities.tools.calendar import CalendarListTool

    tool = CalendarListTool(
        credentials_path=tmp_path / "missing_creds.json", token_path=tmp_path / "token.json"
    )

    with patch(
        "jarvis.capabilities.tools.calendar._load_creds",
        side_effect=FileNotFoundError("Credentials Google manquants : missing_creds.json"),
    ), patch("jarvis.capabilities.tools.calendar.collector") as mock_collector:
        result = await tool.execute(days_ahead=2)

    assert result.is_error
    assert "Erreur credentials" in result.content
    mock_collector.warning.assert_called_once()
    mock_collector.error.assert_not_called()


@pytest.mark.asyncio
async def test_calendar_other_exception_still_logs_error(tmp_path: Path) -> None:
    """Une exception non liée aux credentials manquants reste en ERROR (pas masquée)."""
    from jarvis.capabilities.tools.calendar import CalendarListTool

    tool = CalendarListTool(
        credentials_path=tmp_path / "creds.json", token_path=tmp_path / "token.json"
    )

    with patch(
        "jarvis.capabilities.tools.calendar._load_creds",
        side_effect=RuntimeError("token corrompu"),
    ), patch("jarvis.capabilities.tools.calendar.collector") as mock_collector:
        result = await tool.execute(days_ahead=2)

    assert result.is_error
    mock_collector.error.assert_called_once()
    mock_collector.warning.assert_not_called()


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

    async def execute(self, days_ahead: int = 2, **_: object):  # noqa: ANN401
        idx = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[idx]


def _make_scheduler(calendar_tool: object):
    from jarvis.engine.background.scheduler import Scheduler
    from jarvis.engine.background.notifications import ProactiveQueue
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
        async def execute(self, days_ahead: int = 2, **_: object):  # noqa: ANN401
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
