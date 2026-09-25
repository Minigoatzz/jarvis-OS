# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""LocalBackend — exécution directe sur l'hôte dans le workspace (opt-in explicite)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from loguru import logger

from jarvis.engine.mission.backends.base import BackendResult, ExecutionBackend
from jarvis.kernel.error_collector import collector  # jrv: autofix
from jarvis.kernel.settings import settings
from jarvis.kernel.subprocess_compat import describe_exception, run_shell

# Sur Windows, « python3 » n'existe pas : il tombe sur le stub du Microsoft
# Store, qui ouvre le Store et sort en code non-zéro. « python » nu peut tomber
# sur le même stub. Or tout le vocabulaire du système (whitelist, prompts,
# exemples) dit « python3 ». On réécrit donc le token d'interpréteur vers
# l'interpréteur réellement en train de faire tourner Jarvis, qui existe par
# construction. Windows uniquement : ailleurs « python3 » est correct, et sous
# Docker c'est le DockerBackend qui exécute — il ne passe jamais par ici.
_PY_TOKEN_RE = re.compile(r"(?:(?<=^)|(?<=&&)|(?<=;)|(?<=\|))(\s*)(python3?)(?=\s|$)")


def _resolve_interpreter(command: str) -> str:
    """Remplace les tokens python/python3 par sys.executable sur Windows."""
    if os.name != "nt":
        return command
    exe = shlex.quote(sys.executable) if " " in sys.executable else sys.executable
    return _PY_TOKEN_RE.sub(lambda m: f"{m.group(1)}{exe}", command)


class LocalBackend(ExecutionBackend):
    """Exécution directe dans le workspace hôte.

    Requiert allow_unsandboxed_exec=True dans les settings — refuse sinon.
    La validation whitelist/blacklist reste à la charge de WorkerCLITool en amont.
    """

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path).resolve()

    async def is_available(self) -> bool:

        return bool(getattr(settings, "allow_unsandboxed_exec", False))

    async def execute(self, command: str, timeout: int = 60) -> BackendResult:  # noqa: ASYNC109

        if not getattr(settings, "allow_unsandboxed_exec", False):
            logger.error("LocalBackend: opt-in manquant (ALLOW_UNSANDBOXED_EXEC absent/false)")
            return BackendResult(
                success=False,
                stdout="",
                stderr=(
                    "Exécution directe refusée : ALLOW_UNSANDBOXED_EXEC non activé. "
                    "Activez Docker (DOCKER_ENABLED=true, recommandé) "
                    "ou passez ALLOW_UNSANDBOXED_EXEC=true (déconseillé)."
                ),
                returncode=-1,
                blocked=True,
            )

        resolved = _resolve_interpreter(command)
        try:
            # run_shell passe par un thread : l'API sous-processus de la boucle
            # asyncio n'existe pas sur la SelectorEventLoop qu'uvicorn impose
            # sur Windows quand reload=True. Voir kernel/subprocess_compat.
            result = await run_shell(resolved, cwd=self._workspace, timeout=timeout)
            logger.debug("LocalBackend exec", cmd=resolved[:80], rc=result.returncode)
            return BackendResult(
                success=result.success,
                stdout=result.stdout[:8000],
                stderr=result.stderr[:2000],
                returncode=result.returncode,
            )
        except subprocess.TimeoutExpired:
            collector.error("JRV-MSN-001", "JRV-MSN-001")
            return BackendResult(
                success=False,
                stdout="",
                stderr=f"Timeout après {timeout}s",
                returncode=-1,
            )
        except Exception as exc:
            collector.error("JRV-MSN-001", "JRV-MSN-001", cause=exc)
            return BackendResult(
                success=False,
                stdout="",
                # describe_exception, pas str() : str(NotImplementedError()) est
                # vide, et c'est ce qui a rendu cette panne invisible.
                stderr=describe_exception(exc),
                returncode=-1,
            )
