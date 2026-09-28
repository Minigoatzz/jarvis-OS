# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Route PROJECT : la mission est l'action — tests de COMPORTEMENT du gateway.

Incident du 28/09 : « cree un fichier bonjour.txt contenant la date du jour ».
La route a bien ete forcee I -> BG:PROJECT, mais le modele avait emis
`execute_script` en appel NATIF. Le gateway l'a execute, le chat a affiche
« Fait. Le fichier a ete cree », et la vraie mission (proj_6cf963) a reussi en
silence. L'utilisateur a conclu qu'aucune mission n'existait.

Les tests precedents verifiaient la PRESENCE du texte `and not is_project` dans
le source. Ils passaient — et le bug existait quand meme, parce qu'aucun garde
ne couvrait le chemin natif. D'ou ces tests-ci : ils pilotent reellement
`Gateway.handle()` avec un agent factice et observent ce qui s'execute.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from jarvis.engine.gateway import Gateway
from jarvis.engine.mission import announcements
from jarvis.engine.router import RouteEnum
from jarvis.kernel.schemas import ToolCapture

# ── Doublures ───────────────────────────────────────────────────────────────


@dataclass
class _Session:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    messages: list = field(default_factory=list)

    def add_message(self, role: str, content: str) -> None:
        self.messages.append((role, content))


class _Sessions:
    def __init__(self) -> None:
        self.session = _Session()

    def get_or_create(self, session_id: str | None) -> _Session:
        return self.session


class _Notifications:
    def drain(self) -> list:
        return []

    def add(self, text: str) -> None:
        pass


class _Agent:
    """Agent factice : un premier jet scripte + d'eventuels appels natifs.

    `executed` compte les lancements d'outils — c'est l'espion. `drained`
    dit si le flux du modele a ete consomme jusqu'au bout.
    """

    def __init__(self, first_jet: str, native_calls: list | None = None) -> None:
        self._first_jet = first_jet
        self._native = native_calls or []
        self.executed = 0
        self.forced = 0
        self.drained = False

    def start_routing_stream(self, **_: object) -> tuple[AsyncIterator[str], ToolCapture]:
        capture = ToolCapture()
        agent = self

        async def _stream() -> AsyncIterator[str]:
            for word in self._first_jet.split(" "):
                yield word + " "
            capture.calls.extend(self._native)  # comme un provider en streaming
            agent.drained = True

        return _stream(), capture

    async def execute_captured_tools(self, capture: ToolCapture) -> list:
        self.executed += 1
        return ["ok" for _ in capture.calls]  # l'agent reel rend des chaines

    def extract_text_tool_calls(self, text: str) -> list:
        return [("t1", "execute_cli", {"command": "echo x"})] if "execute_cli(" in text else []

    def strip_text_tool_calls(self, text: str) -> str:
        return text

    async def force_tool_call(self, message: str) -> list:
        self.forced += 1
        return []

    def synthesize(self, *a: object, **k: object) -> AsyncIterator[str]:
        async def _s() -> AsyncIterator[str]:
            yield "[I] synthese"

        return _s()


def _run(agent: _Agent, message: str) -> tuple[RouteEnum, str]:
    gateway = Gateway(_Sessions(), agent, _Notifications(), worker=None)  # type: ignore[arg-type]

    async def _go() -> tuple[RouteEnum, str]:
        _, route, response = await gateway.handle(message=message, stream=True)
        text = "".join([c async for c in response])  # type: ignore[union-attr]
        return route, text

    return asyncio.run(_go())


# ── 1. Le cas exact du 28/09 ────────────────────────────────────────────────


def test_un_appel_natif_n_est_jamais_execute_en_route_mission() -> None:
    agent = _Agent(
        "[I] Fait. Le fichier bonjour.txt a été créé avec la date du jour.",
        native_calls=[("n1", "execute_script", {"script": "open('bonjour.txt','w')"})],
    )
    route, text = _run(agent, "crée un fichier bonjour.txt contenant la date du jour")

    assert route is RouteEnum.PROJECT, "la route devait etre forcee en mission"
    assert agent.executed == 0, "execute_script a tourne EN PLUS de la mission"


def test_le_chat_annonce_la_mission_au_lieu_de_mentir() -> None:
    """« Fait. » etait faux : a ce moment la mission n'existe meme pas encore."""
    agent = _Agent("[I] Fait. Le fichier bonjour.txt a été créé.")
    _, text = _run(agent, "crée un fichier bonjour.txt contenant la date du jour")

    assert text == announcements.launch_ack()
    assert "Fait" not in text


def test_un_appel_ecrit_en_texte_n_est_pas_execute_non_plus() -> None:
    agent = _Agent('[BG:PROJECT] Je lance. execute_cli(command="echo x")')
    route, _ = _run(agent, "lance une mission : crée un fichier notes.txt")

    assert route is RouteEnum.PROJECT
    assert agent.executed == 0
    assert agent.forced == 0, "la relance d'outil n'a aucun sens en route mission"


def test_le_flux_du_modele_est_vide_jusqu_au_bout() -> None:
    """Sinon la connexion au LLM resterait ouverte jusqu'au ramasse-miettes."""
    agent = _Agent("[I] Fait.", native_calls=[("n1", "execute_script", {})])
    _run(agent, "crée un fichier bonjour.txt")
    assert agent.drained


# ── 2. Non-regression : les autres routes executent toujours ────────────────


def test_une_action_du_chat_execute_toujours_son_outil() -> None:
    """La couture PROJECT ne doit rien retirer au chemin [CF]."""
    agent = _Agent(
        "[CF] Je lance Red House.",
        native_calls=[("n1", "spotify_control", {"action": "play"})],
    )
    route, text = _run(agent, "joue Red House")

    assert route is RouteEnum.CONFIRM_FIRE
    assert agent.executed == 1
    assert "synthese" in text


@pytest.mark.parametrize("message", ["joue Red House", "quelle heure il est"])
def test_la_route_mission_n_est_pas_forcee_hors_livrable(message: str) -> None:
    agent = _Agent("[I] Il est 14h.")
    route, _ = _run(agent, message)
    assert route is not RouteEnum.PROJECT
