# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Outil `mission_control` — piloter les missions [BG:PROJECT] à la voix.

Pourquoi il existe : l'orchestrateur expose déjà `kill()`, `retry_project()` et
`list_projects()`, mais AUCUN outil ne les exposait au modèle. Demander « cancel
mes deux missions » ne menait donc nulle part — et le modèle, faute de moyen
légitime, a improvisé `execute_cli(command="killall -9 python3")`, c'est-à-dire
tuer tous les processus Python de la machine, Jarvis compris. L'allowlist
binaire de execute_cli l'a refusé (killall n'y figure pas), mais la leçon tient :
une capacité réelle sans outil pour l'atteindre pousse le modèle vers le
contournement le plus destructeur à portée.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from jarvis.capabilities.tools.base import Tool, ToolResult
from jarvis.kernel.schemas import ProjectStatus

if TYPE_CHECKING:  # pragma: no cover - uniquement pour le typage
    from jarvis.engine.mission.orchestrator import ProjectOrchestrator
    from jarvis.kernel.schemas import Project

# Une mission dans un de ces états est « en vol » : annulable, et comptée comme
# active quand l'utilisateur dit « mes missions » sans préciser laquelle.
_ACTIVE = (ProjectStatus.PLANNING, ProjectStatus.RUNNING, ProjectStatus.PAUSED)


def _describe(project: Project) -> str:
    done = sum(1 for s in project.steps if s.status == "done")
    total = len(project.steps)
    return f"{project.title} ({project.id}) — {project.status} — {done}/{total} étapes"


class MissionControlTool(Tool):
    name = "mission_control"
    description = (
        "Pilote les missions en cours (les projets lancés via [BG:PROJECT]).\n\n"
        "Utilise cet outil quand l'utilisateur demande :\n"
        '- "annule ma mission" / "cancel mes missions" → action: cancel\n'
        '- "où en sont mes missions ?" / "statut des missions" → action: status\n'
        '- "relance la mission" → action: retry\n\n'
        "Sans project_id, cancel et retry visent la mission active la plus "
        'récente. Pour tout annuler d\'un coup : action: cancel, target: "all".'
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "cancel", "retry"],
                "description": "Action à effectuer sur les missions.",
            },
            "project_id": {
                "type": "string",
                "description": "Identifiant précis de la mission (optionnel).",
            },
            "target": {
                "type": "string",
                "enum": ["latest", "all"],
                "description": "Sans project_id : la plus récente (défaut) ou toutes.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, orchestrator: ProjectOrchestrator) -> None:
        self._orch = orchestrator

    # ── Sélection ───────────────────────────────────────────────────────────

    def _active(self) -> list[Project]:
        """Missions en vol, la plus récente d'abord."""
        projects = [p for p in self._orch.list_projects() if p.status in _ACTIVE]
        return sorted(projects, key=lambda p: p.created_at, reverse=True)

    # ── Exécution ───────────────────────────────────────────────────────────

    async def execute(  # type: ignore[override]
        self,
        action: str,
        project_id: str | None = None,
        target: str = "latest",
        **_: object,
    ) -> ToolResult:
        if action == "status":
            return self._status()
        if action == "cancel":
            return self._cancel(project_id, target)
        if action == "retry":
            return await self._retry(project_id)
        return ToolResult(content=f"Action inconnue : {action}", is_error=True)

    def _status(self) -> ToolResult:
        projects = sorted(
            self._orch.list_projects(), key=lambda p: p.created_at, reverse=True
        )[:8]
        if not projects:
            return ToolResult(content="Aucune mission enregistrée.")
        lines = "\n".join(f"- {_describe(p)}" for p in projects)
        active = len([p for p in projects if p.status in _ACTIVE])
        return ToolResult(content=f"{active} mission(s) en vol.\n{lines}")

    def _cancel(self, project_id: str | None, target: str) -> ToolResult:
        if project_id:
            if self._orch.kill(project_id):
                logger.info("Mission annulée", project_id=project_id)
                return ToolResult(content=f"Mission {project_id} annulée.")
            return ToolResult(
                content=(
                    f"Mission {project_id} introuvable ou déjà terminée — "
                    "rien à annuler."
                ),
                is_error=True,
            )

        active = self._active()
        if not active:
            return ToolResult(content="Aucune mission en vol — rien à annuler.")

        chosen = active if target == "all" else active[:1]
        killed = [p for p in chosen if self._orch.kill(p.id)]
        if not killed:
            # Présentes dans le store mais sans worker vivant : rien à tuer.
            return ToolResult(
                content=(
                    f"{len(chosen)} mission(s) listée(s) comme actives mais aucun "
                    "worker à arrêter — elles étaient déjà terminées."
                ),
                is_error=True,
            )
        logger.info("Missions annulées", ids=[p.id for p in killed])
        titles = ", ".join(p.title for p in killed)
        return ToolResult(content=f"{len(killed)} mission(s) annulée(s) : {titles}.")

    async def _retry(self, project_id: str | None) -> ToolResult:
        if not project_id:
            recent = sorted(
                self._orch.list_projects(), key=lambda p: p.created_at, reverse=True
            )
            failed = [p for p in recent if p.status == ProjectStatus.FAILED]
            if not failed:
                return ToolResult(content="Aucune mission échouée à relancer.")
            project_id = failed[0].id

        project = await self._orch.retry_project(project_id)
        if project is None:
            return ToolResult(
                content=f"Mission {project_id} introuvable — relance impossible.",
                is_error=True,
            )
        logger.info("Mission relancée", project_id=project_id)
        return ToolResult(content=f"Mission « {project.title} » relancée.")
