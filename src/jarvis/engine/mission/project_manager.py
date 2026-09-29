# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""ProjectManager — analyse une mission et la décompose en étapes exécutables."""

from __future__ import annotations

from loguru import logger

from jarvis.engine.mission.plan_normalizer import (
    extract_plan_json,
    normalize_plan,
    renumber_steps,
)
from jarvis.engine.mission.project_store import ProjectStore
from jarvis.engine.mission.schemas import Project, Step, StepStatus
from jarvis.engine.vocab import AccessLevel
from jarvis.kernel.contracts import LLMProvider

_PLANNING_SYSTEM = """\
Tu es un chef de projet. Découpe la demande de l'utilisateur en étapes qu'un agent
autonome exécutera dans un workspace isolé.

Règles :
- Le MOINS d'étapes possible. La plupart des demandes en demandent 1 à 3 ; jamais plus de 6.
- Une étape = un livrable cohérent. Un fichier s'écrit EN ENTIER en une étape : jamais
  « écrire la fonction X » puis « ajouter le code principal ».
- Ne planifie AUCUNE étape de test, de vérification, de validation ni de rapport : le
  moteur vérifie lui-même le résultat final. Exception : si la demande dit d'exécuter
  un programme, une étape l'exécute.

Boîte à outils de l'agent — ne planifie rien qui en sorte :
- read_file / write_file / list_files / create_directory, relatifs au workspace
- delete_file : supprimer un fichier (l'utilisateur approuve chaque suppression)
- execute_cli, limité à une whitelist : python, node, npm, git (sans push ni commit),
  pip install, mkdir, cp, mv, curl -s, wget, ffmpeg, zip, unzip, pandoc
- AUCUN navigateur, AUCUN envoi d'email, AUCUN accès hors du workspace

Fusion 360 (demande de CAO, modélisation 3D, STL, pièce 3D) :
- Chaque étape décrit EXACTEMENT une opération faite avec l'outil fusion_360
  (sketch, extrusion, fillet, shell, export STL…), jamais avec write_file ni execute_cli.
- Scripts Fusion : adsk.core / adsk.fusion, en CENTIMÈTRES (10 mm → createByReal(1)).
- Un screenshot après chaque opération importante.

Champs :
- requires_network : true seulement si internet est nécessaire (pip/npm install, API).
- success_criterion : en une phrase, ce que « étape terminée » veut dire.
- access_level : 0 lecture seule · 1 écrire dans le workspace (défaut) · 2 exécuter du
  code · 3 réseau · 4 installer un paquet (approbation humaine).

Réponds UNIQUEMENT avec ce JSON, sans markdown :
{
  "title": "Titre court (< 40 caractères)",
  "requires_network": false,
  "steps": [
    {
      "id": "step_001",
      "title": "Titre de l'étape (< 50 caractères)",
      "description": "Ce que l'agent doit faire (1 à 3 phrases)",
      "success_criterion": "Ce que « terminé » veut dire",
      "access_level": 1
    }
  ]
}
"""


class ProjectManager:
    def __init__(self, llm: LLMProvider) -> None:
        self._store = ProjectStore()
        self._llm = llm

    async def create_project(self, mission: str, timeout_minutes: int = 30) -> Project:
        logger.info("ProjectManager planning", mission=mission[:80])

        raw = await self._llm.complete(
            messages=[{"role": "user", "content": f"Mission : {mission}"}],
            system=_PLANNING_SYSTEM,
            stream=False,
        )
        # Frontière LLM → moteur : tout ce qui suit a été vérifié et typé par
        # plan_normalizer. Plus de step["id"] en dur, plus de .strip() sur None.
        plan = normalize_plan(extract_plan_json(str(raw or "")), mission=mission)
        plan["steps"] = renumber_steps(plan["steps"])
        project = self._store.create_project(
            mission=mission,
            title=plan["title"],
            timeout_minutes=timeout_minutes,
        )
        project.requires_network = plan["requires_network"]

        for step_data in plan["steps"]:
            project.steps.append(
                Step(
                    id=step_data["id"],
                    title=step_data["title"],
                    description=step_data["description"],
                    requires_approval=step_data["requires_approval"],
                    status=StepStatus.PENDING,
                    # PHASE 1 — champs vérification & gouvernance (§3.4)
                    success_criterion=step_data["success_criterion"],
                    access_level=AccessLevel(step_data["access_level"]),
                )
            )

        project.llm_calls += 1
        self._store.save_project(project)
        logger.info(
            "Project created",
            id=project.id,
            steps=len(project.steps),
            requires_network=project.requires_network,
        )
        return project

    def _parse_plan(self, raw: str) -> dict:
        """Compatibilité : délègue à plan_normalizer.extract_plan_json."""
        return extract_plan_json(raw)
