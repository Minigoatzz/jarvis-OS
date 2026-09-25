# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""LocalBackend — exécution directe sur l'hôte dans le workspace (opt-in explicite)."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
from pathlib import Path

from loguru import logger

from jarvis.engine.mission.backends.base import BackendResult, ExecutionBackend
from jarvis.kernel.error_collector import collector  # jrv: autofix
from jarvis.kernel.settings import settings

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

        try:
            resolved = _resolve_interpreter(command)
            proc = await asyncio.create_subprocess_shell(
                resolved,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workspace,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            logger.debug("LocalBackend exec", cmd=resolved[:80], rc=proc.returncode)
            return BackendResult(
                success=proc.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace")[:8000],
                stderr=stderr.decode("utf-8", errors="replace")[:2000],
                returncode=proc.returncode,
            )
        except TimeoutError:
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
                stderr=str(exc),
                returncode=-1,
            )
