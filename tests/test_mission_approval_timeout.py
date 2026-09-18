# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — une approbation sans réponse n'est pas un refus.

Bug réel, visible dans le dashboard : deux étapes marquées SKIPPED avec
« Refusée par l'utilisateur. » alors qu'aucune demande n'avait jamais été
affichée à l'écran.

Chaîne : `WorkerAgent._execute_step` appelle `_approval_cb`, qui diffuse un
événement WebSocket `approval_request` puis attend une réponse pendant 600 s.
Le frontend n'écoute pas cet événement et n'appelle jamais
`POST /api/projects/{id}/approve` — la seule route qui résout l'attente. Au bout
de dix minutes, `asyncio.wait_for` lève TimeoutError, l'ancien code renvoyait
`False`, et `False` était écrit comme un refus humain.

Le correctif sépare les trois cas : True = approuvé, False = refusé,
None = personne n'a répondu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.engine.mission.schemas import StepStatus

if TYPE_CHECKING:
    from jarvis.engine.mission.worker_agent import WorkerAgent


def _make_worker(decision: bool | None) -> WorkerAgent:
    """WorkerAgent minimal dont le callback d'approbation rend `decision`."""
    from jarvis.engine.mission.worker_agent import WorkerAgent

    project = MagicMock()
    project.id = "PROJ_T"
    project.workspace_path = "/tmp/jarvis-test-workspace"
    project.mission = "test"
    project.title = "test"

    async def _cb(*_a: object, **_k: object) -> bool | None:
        return decision

    worker = WorkerAgent(
        project=project,
        store=MagicMock(),
        broadcast_event=MagicMock(),
        approval_callback=_cb,  # type: ignore[arg-type]
        llm=MagicMock(),
    )
    worker._push_update = MagicMock()  # type: ignore[method-assign]
    worker._log = AsyncMock()  # type: ignore[method-assign]
    return worker


def _step() -> MagicMock:
    step = MagicMock()
    step.id = "s1"
    step.title = "Obtenir la clé d'API"
    step.description = "Obtenir la clé d'API"
    step.requires_approval = True
    step.status = None
    step.output = None
    return step


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_fragment", "forbidden_fragment"),
    [
        (None, "Aucune réponse", "Refusée par l'utilisateur"),
        (False, "Refusée par l'utilisateur", "Aucune réponse"),
    ],
)
async def test_no_answer_is_not_recorded_as_a_refusal(
    decision: bool | None, expected_fragment: str, forbidden_fragment: str
) -> None:
    worker = _make_worker(decision)
    step = _step()

    with patch.object(worker, "_gate_step", AsyncMock(return_value=None)):
        await worker._execute_step(step)

    assert step.status == StepStatus.SKIPPED
    assert expected_fragment in step.output
    assert forbidden_fragment not in step.output


@pytest.mark.asyncio
async def test_orchestrator_returns_none_when_nobody_answers() -> None:
    """Le timeout doit rendre None, pas False — c'est toute la distinction."""
    import asyncio

    from jarvis.engine.mission.orchestrator import ProjectOrchestrator

    orch = ProjectOrchestrator.__new__(ProjectOrchestrator)
    orch._pending_approvals = {}  # type: ignore[attr-defined]
    orch._broadcast = MagicMock()  # type: ignore[attr-defined]

    real_wait_for = asyncio.wait_for

    async def _instant_timeout(*_a: object, **_k: object) -> object:
        """Remplace asyncio.wait_for et expire immédiatement."""
        return await real_wait_for(asyncio.sleep(3600), timeout=0.01)

    with patch("jarvis.engine.mission.orchestrator.asyncio.wait_for", _instant_timeout):
        result = await orch._request_approval("PROJ_T", "s1", "faire un truc")

    assert result is None, "un timeout rendu False se lit comme un refus humain"


def test_the_resolve_route_exists_but_nothing_calls_it() -> None:
    """Garde-fou documentaire : la route backend existe.

    Si ce test échoue, c'est que la route a bougé — et le front, qui devra
    l'appeler une fois l'UI d'approbation câblée, la cherchera au mauvais endroit.
    """
    from pathlib import Path

    api_dir = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "interfaces" / "api"
    projects_api = api_dir / "projects.py"
    source = projects_api.read_text(encoding="utf-8")

    assert "resolve_approval" in source


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
