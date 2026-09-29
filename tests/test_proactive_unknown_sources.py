# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Une source NON LUE n'est pas une source VIDE.

Incident du 29/09 : sans credentials Google, le collecteur d'agenda rendait [] ;
le resume disait « Agenda libre dans les 48h », et le moteur proactif en tirait
l'initiative « Agenda libre disponible » — sur un agenda que personne n'avait lu.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from jarvis.capabilities.tools.base import ToolResult
from jarvis.engine.proactive.collectors.base import CollectorBase, SourceUnavailable
from jarvis.engine.proactive.collectors.calendar import CalendarCollector
from jarvis.engine.proactive.collectors.tasks import TaskCollector
from jarvis.engine.proactive.context_builder import ContextBuilder

_SANS_CREDENTIALS = ToolResult(content="Credentials Google manquants", is_error=True)


def _outil(result: ToolResult) -> MagicMock:
    outil = MagicMock()
    outil.execute = AsyncMock(return_value=result)
    return outil


def _etat(calendrier: ToolResult, taches: ToolResult):  # noqa: ANN202
    builder = ContextBuilder(calendar_tool=_outil(calendrier), notion_tool=_outil(taches))
    # Seulement les sources sous test : pas de reseau (email, meteo, actualites).
    builder._collectors = [
        CalendarCollector(calendar_tool=_outil(calendrier)),
        TaskCollector(notion_tool=_outil(taches)),
    ]
    return asyncio.run(builder.build())


def test_un_agenda_illisible_n_est_jamais_libre() -> None:
    etat = _etat(_SANS_CREDENTIALS, _SANS_CREDENTIALS)

    assert "libre" not in etat.calendar_summary.lower()
    assert "INCONNU" in etat.calendar_summary
    assert "Credentials Google manquants" in etat.calendar_summary
    # La RAISON, pas seulement le mot : une NameError absorbee passerait aussi
    # pour « INCONNU » — et c'est exactement ce qui a failli arriver.
    assert "Credentials Google manquants" in etat.tasks_summary
    assert set(etat.collection.errors) == {"calendar", "tasks"}


def test_un_agenda_lu_et_vide_reste_libre() -> None:
    etat = _etat(ToolResult(content=""), ToolResult(content=""))

    assert etat.calendar_summary == "Agenda libre dans les 48h."
    assert etat.tasks_summary == "Aucune tâche en cours."
    assert etat.collection.errors == {}


def test_collect_ne_leve_jamais_et_note_la_raison() -> None:
    class _Source(CollectorBase):
        name = "x"
        lisible = False

        async def _collect(self) -> list:
            if not self.lisible:
                raise SourceUnavailable("non configurée")
            return []

    source = _Source()
    assert asyncio.run(source.collect()) == []
    assert source.last_error == "non configurée"

    source.lisible = True
    asyncio.run(source.collect())
    assert source.last_error is None, "une lecture reussie efface l'ancienne raison"
