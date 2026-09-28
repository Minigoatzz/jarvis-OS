# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Routage explicite des missions et vue des chemins par backend.

« lance une mission : cree un fichier bonjour.txt » repartait en [CF] et
finissait dans execute_script. Le tag de routage est emis par le LLM, et un
modele local de 14B n'est pas fiable sur une intention pourtant ecrite noir
sur blanc par l'utilisateur.

En prime, le runner RPC reecrivait toujours le chemin du workspace vers
/workspace — le point de montage du container — meme en execution locale,
d'ou « C:\\workspace\\.jarvis_rpc\\... : No such file or directory ».
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.engine.mission.backends.docker import DockerBackend
from jarvis.engine.mission.backends.local import LocalBackend
from jarvis.engine.router import SpeedRouter
from jarvis.kernel.subprocess_compat import describe_exception, run_exec

# ── Une mission demandee explicitement est une mission ──────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "lance une mission : crée un fichier bonjour.txt contenant la date du jour",
        "crée une mission pour rédiger 3 emails",
        "démarre un projet agent",
        "mission : construire une landing page",
        "lance moi une mission",
        "nouvelle mission",
    ],
)
def test_une_demande_explicite_de_mission_est_reconnue(message: str) -> None:
    assert SpeedRouter.explicit_project(message)


@pytest.mark.parametrize(
    "message",
    [
        "montre-moi la mission en cours",
        "supprime la mission PROJ_5",
        "où en est la mission ?",
        "liste les missions",
        "relance la mission",
        "crée trois fichiers de test",
        "joue Red House",
        "lance la musique",
        "quelle est la mission de cette entreprise",
    ],
)
def test_consulter_ou_agir_sur_une_mission_n_en_cree_pas_une(message: str) -> None:
    assert not SpeedRouter.explicit_project(message)


def test_le_gateway_force_la_route_projet() -> None:
    """Sans ça, le tag du modèle décide seul — et il se trompait."""
    import pathlib

    source = pathlib.Path("src/jarvis/engine/gateway.py").read_text(encoding="utf-8")
    assert "SpeedRouter.explicit_project(message)" in source
    assert "route = RouteEnum.PROJECT" in source


# ── Chaque backend expose sa propre vue des chemins ─────────────────────────


def test_le_backend_local_ne_traduit_aucun_chemin() -> None:
    backend = LocalBackend(r"C:/jarvis-OS/workspace")
    chemin = r"C:/jarvis-OS/workspace/.jarvis_rpc/ab12"
    assert backend.map_path(chemin) == chemin


def test_le_backend_docker_traduit_vers_le_point_de_montage() -> None:
    backend = DockerBackend(None, r"C:/jarvis-OS/workspace")
    assert backend.map_path(r"C:/jarvis-OS/workspace/.jarvis_rpc/ab12") == (
        "/workspace/.jarvis_rpc/ab12"
    )


def test_le_runner_rpc_demande_le_chemin_au_backend() -> None:
    """Il supposait Docker et cassait toute exécution locale sur Windows."""
    import pathlib

    source = pathlib.Path("src/jarvis/engine/mission/backends/rpc.py").read_text(
        encoding="utf-8"
    )
    assert "self._backend.map_path(str(rpc_dir))" in source
    assert 'replace(str(self._workspace), "/workspace")' not in source


def test_le_script_rpc_est_lance_entre_guillemets() -> None:
    """Un chemin Windows contient « C:\\Users\\... » et peut avoir des espaces."""
    import pathlib

    source = pathlib.Path("src/jarvis/engine/mission/backends/rpc.py").read_text(
        encoding="utf-8"
    )
    assert 'f\'python3 "{script_path}"\'' in source


# ── Un binaire absent doit se nommer ────────────────────────────────────────


def test_un_binaire_introuvable_est_nomme() -> None:
    """« [WinError 2] The system cannot find the file specified » ne dit pas quoi."""
    with pytest.raises(FileNotFoundError) as info:
        asyncio.run(run_exec(["binaire_absent_xyz", "-x"]))

    message = describe_exception(info.value)
    assert "binaire_absent_xyz" in message
    assert "introuvable" in message.lower()
