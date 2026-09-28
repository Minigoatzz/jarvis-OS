# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Lancement de sous-processus independant de la boucle asyncio.

Le probleme
-----------
`asyncio.create_subprocess_shell/_exec` exige, sur Windows, une
ProactorEventLoop. Or uvicorn choisit une SelectorEventLoop des que
`reload=True` (il reserve le Proactor au cas sans sous-processus). Comme
`ENVIRONMENT=development` active `reload`, le serveur API tourne sur une
boucle qui ne sait PAS lancer de sous-processus : chaque appel leve
`NotImplementedError`.

Consequence observee : execute_cli et execute_script echouaient
systematiquement sur Windows, en chat comme en mission — et en silence,
parce que `str(NotImplementedError())` vaut la chaine vide (voir
`describe_exception`).

La solution
-----------
On n'utilise plus l'API sous-processus de la boucle. Un `subprocess.run`
bloquant est execute dans un thread via `asyncio.to_thread`. C'est agnostique
a la boucle : Selector, Proactor, uvloop, avec ou sans reload, Windows ou
POSIX. Le comportement ne depend plus d'un detail de configuration du serveur.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_TIMEOUT = 60


def describe_exception(exc: BaseException) -> str:
    """Message d'exception jamais vide.

    `str(NotImplementedError())` vaut "". Toute une classe de pannes est
    restee invisible pour cette seule raison : l'interface affichait
    « Erreur outil execute_cli: » suivi de rien. On retombe sur le nom de
    la classe quand l'exception ne porte pas de message.
    """
    message = str(exc).strip()
    return message or type(exc).__name__


@dataclass(frozen=True)
class RunResult:
    """Resultat normalise d'un sous-processus."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


def _run_blocking(
    command: str | list[str],
    *,
    shell: bool,
    cwd: str | Path | None,
    env: dict[str, str] | None,
    timeout: int,
) -> RunResult:
    """Appel bloquant, execute dans un thread par les fonctions publiques."""
    try:
        completed = subprocess.run(  # noqa: S602,S603 — whitelist appliquee en amont
            command,
            shell=shell,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    # Cas prevu et traduit, pas une panne a collecter : on relaie l'erreur
    # en y ajoutant le nom du binaire manquant.
    except FileNotFoundError as exc:  # jrv: pas de code — cas prevu
        # « [WinError 2] The system cannot find the file specified » ne dit pas
        # QUEL fichier. Sur Windows c'est presque toujours un binaire POSIX
        # absent (touch, cat, ls, grep...) : sans le nom, l'utilisateur comme
        # le modele sont incapables de corriger.
        binaire = command if isinstance(command, str) else (command[0] if command else "?")
        raise FileNotFoundError(
            f"Commande introuvable : '{binaire}'. "
            f"Ce binaire n'existe pas sur cette machine."
        ) from exc

    def _decode(raw: bytes | str | None) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        return raw.decode("utf-8", errors="replace")

    return RunResult(
        returncode=completed.returncode,
        stdout=_decode(completed.stdout),
        stderr=_decode(completed.stderr),
    )


async def run_shell(
    command: str,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — va a subprocess.run
) -> RunResult:
    """Execute une ligne de commande via le shell, sur n'importe quelle boucle."""
    return await asyncio.to_thread(
        _run_blocking, command, shell=True, cwd=cwd, env=env, timeout=timeout
    )


async def run_exec(
    parts: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — va a subprocess.run
) -> RunResult:
    """Execute un binaire et ses arguments sans shell, sur n'importe quelle boucle."""
    return await asyncio.to_thread(
        _run_blocking, parts, shell=False, cwd=cwd, env=env, timeout=timeout
    )
