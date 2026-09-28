# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""DockerBackend — délègue l'exécution au DockerExecutor déjà démarré."""

from __future__ import annotations

from loguru import logger

from jarvis.engine.mission.backends.base import BackendResult, ExecutionBackend
from jarvis.engine.mission.docker_executor import DockerExecutor
from jarvis.kernel.settings import settings


class DockerBackend(ExecutionBackend):
    """Wraps un DockerExecutor existant (démarré par worker_agent ou externalement).

    Respecte tous les garde-fous de DockerExecutor :
    --rm, cap-drop ALL, no-new-privileges, mémoire et CPU limités.
    """

    # Point de montage du workspace dans le container (cf. DockerExecutor).
    WORKSPACE_MOUNT = "/workspace"

    def __init__(self, executor: object, workspace_path: str | None = None) -> None:
        self._executor = executor  # instance DockerExecutor
        self._workspace = workspace_path or str(getattr(executor, "_workspace", "") or "")

    def map_path(self, host_path: str) -> str:
        """Le workspace hote est monte sous /workspace dans le container."""
        if not self._workspace:
            return str(host_path)
        mapped = str(host_path).replace(str(self._workspace), self.WORKSPACE_MOUNT)
        return mapped.replace("\\", "/")

    async def is_available(self) -> bool:

        return settings.docker_enabled and await DockerExecutor.is_available()

    async def execute(self, command: str, timeout: int = 60) -> BackendResult:  # noqa: ASYNC109
        if not self._executor:
            logger.error("DockerBackend: executor non initialisé")
            return BackendResult(
                success=False,
                stdout="",
                stderr="DockerBackend : executor non démarré.",
                returncode=-1,
            )

        result: dict = await self._executor.execute(command, timeout)
        return BackendResult(
            success=result["success"],
            stdout=result["stdout"],
            stderr=result["stderr"],
            returncode=result["returncode"],
        )
