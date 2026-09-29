# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""WorkerAgent — exécute les étapes d'un projet avec un vrai tool_loop Anthropic."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from loguru import logger

from jarvis.engine.audit import AuditLog
from jarvis.engine.budget import BudgetGuard
from jarvis.engine.mission import announcements as mission_announcements
from jarvis.engine.mission.docker_executor import DockerExecutor
from jarvis.engine.mission.file_tool import SandboxedFileTool
from jarvis.engine.mission.governance import GateContext, GateDecision, Governance
from jarvis.engine.mission.project_store import ProjectStore
from jarvis.engine.mission.quality_checker import QualityChecker
from jarvis.engine.mission.reflexion import Reflexion
from jarvis.engine.mission.schemas import LogEntry, Project, ProjectStatus, Step, StepStatus
from jarvis.engine.mission.verifier import Acceptance, Verifier
from jarvis.engine.mission.worker_cli import WorkerCLITool
from jarvis.engine.vocab import AccessLevel
from jarvis.kernel.approvals import approval_config
from jarvis.kernel.contracts import LLMProvider
from jarvis.kernel.error_collector import collector  # jrv: autofix
from jarvis.kernel.errors import BudgetExceeded
from jarvis.kernel.events import EventBus, MissionCompleted
from jarvis.kernel.paths import PROMPTS_DIR
from jarvis.kernel.settings import settings
from jarvis.kernel.subprocess_compat import describe_exception

# ── Constantes PHASE 1 ─────────────────────────────────────────────────────────

# Nombre maximum de tentatives de vérification d'un step (CDC §4.4).
# Essais par étape quand un défaut OBJECTIF est constaté.
_STEP_ATTEMPTS = 2

# Mapping outil → (AccessLevel, action_category) pour le gate au niveau tool (Q3=c, §9).
# Catégorie par défaut "agent_mission" : la mission est l'enveloppe sémantique du worker.
# Un tool futur à effet externe (send_email, etc.) pourra utiliser une catégorie spécifique.
_TOOL_ACCESS_LEVEL: dict[str, AccessLevel] = {
    "read_file": AccessLevel.READ_ONLY,
    "list_files": AccessLevel.READ_ONLY,
    "write_file": AccessLevel.WRITE_LOCAL,
    "create_directory": AccessLevel.WRITE_LOCAL,
    "delete_file": AccessLevel.WRITE_LOCAL,
    "execute_cli": AccessLevel.EXECUTE_CODE,
    "fusion_360": AccessLevel.WRITE_LOCAL,
}
_TOOL_CATEGORY: dict[str, str] = {
    "read_file": "agent_mission",
    "list_files": "agent_mission",
    "write_file": "agent_mission",
    "create_directory": "agent_mission",
    # file_delete vaut ASK par défaut (kernel/approvals.py) et le gate retient la
    # décision la plus restrictive : chaque suppression te demande ton accord.
    "delete_file": "file_delete",
    "execute_cli": "agent_mission",
    "fusion_360": "agent_mission",
}

_QUALITY_RULES_PATH = PROMPTS_DIR / "worker_system.md"
try:
    _QUALITY_RULES = _QUALITY_RULES_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    collector.error("JRV-MSN-001", "JRV-MSN-001")
    _QUALITY_RULES = ""

_WORKER_SYSTEM = """\
Tu es un agent autonome expert qui exécute une étape précise d'un projet dans un workspace isolé.

Outils disponibles :
- read_file(path) : lire un fichier du workspace
- write_file(path, content) : créer ou modifier un fichier
- list_files(directory) : lister les fichiers (directory optionnel, défaut ".")
- execute_cli(command, timeout?) : exécuter une commande shell (whitelist stricte)
- create_directory(path) : créer un répertoire (jamais un nom de fichier)
- delete_file(path) : supprimer un fichier — l'utilisateur doit l'approuver.
  N'affirme JAMAIS qu'un fichier est supprimé sans avoir appelé delete_file
  et reçu « Supprimé : … ». Si la suppression est refusée, dis-le.
- fusion_360(action, ...) : contrôler Autodesk Fusion 360 (si le projet l'exige)
  - action="execute_script", script="..." : exécuter un script Python Fusion API
  - action="read", query_type="screenshot" : capturer la vue actuelle
  - action="undo" / action="redo"
  IMPORTANT : les scripts doivent contenir def run(context): et utiliser adsk.core/adsk.fusion.
  Fusion utilise les centimètres (3 cm → createByReal(3)). Vérifier avec un screenshot après.
  OBLIGATOIRE : chercher un doc avec bRepBodies.count > 0 et l'activer en début de script.
  Si aucun body trouvé, travailler sur l'actif — JAMAIS app.documents.add() !
  Ne jamais supposer que app.activeProduct est le bon document.
  INTERDIT : root.name, rootComponent.name (lecture seule), addNewComponent() (mode Part).
  Nommer avec body.name = "..." uniquement. Mode Pièce : travailler sur rootComponent.
  Shell : top_face = max(body.faces, key=lambda f: f.centroid.z) — jamais par index.
  Cut (CutFeatureOperation) : "Aucun corps cible" = sketch sur mauvais plan ou
    participantBodies absent. Sketch sur une face du body, pas xYConstructionPlane.
    inp.participantBodies = ObjectCollection contenant le body cible — OBLIGATOIRE.

Règles absolues :
- Exécute UNIQUEMENT l'étape demandée
- write_file écrit le contenu TEL QUEL : aucune substitution n'est interprétée.
  $(date), %DATE%, ${{VAR}} finiraient écrits mot pour mot dans le fichier.
  Écris la valeur littérale — la date du jour est donnée dans le contexte.
- Pour les tâches Fusion 360 : utilise fusion_360, JAMAIS execute_cli
- Ne tente jamais d'accéder à des fichiers hors du workspace
- Si un outil échoue, analyse l'erreur et adapte-toi ou retourne une erreur claire
- Retourne UN RÉSUMÉ D'UNE LIGNE maximum — pas de markdown, pas de tableaux, pas de sections
- Ne relis pas les fichiers que tu viens de créer sauf si tu as besoin de leur contenu pour la suite
- Ne recrée pas des répertoires qui existent déjà
- Commence directement par l'action principale (write_file, execute_cli, fusion_360)
- INTERDIT : générer des rapports, tableaux markdown, ou analyses détaillées dans ta réponse finale

Contexte projet :
{context}
"""

_WORKER_TOOLS: list[dict] = [
    {
        "name": "read_file",
        "description": "Lire le contenu d'un fichier dans le workspace",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Chemin relatif au workspace"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Créer ou écraser un fichier dans le workspace",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "Lister les fichiers dans un répertoire du workspace",
        "input_schema": {
            "type": "object",
            "properties": {"directory": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "execute_cli",
        "description": "Exécuter une commande shell (whitelist stricte). Retourne stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "default": 60},
            },
            "required": ["command"],
        },
    },
    {
        "name": "delete_file",
        "description": (
            "Supprimer un fichier du workspace. Demande l'approbation de l'utilisateur. "
            "Ne supprime jamais un dossier."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Chemin relatif au workspace"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "create_directory",
        "description": "Créer un répertoire dans le workspace",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "fusion_360",
        "description": (
            "Contrôle Autodesk Fusion 360 via MCP (port 27182). "
            "Utiliser pour toute tâche de modélisation 3D. "
            "Les scripts doivent contenir def run(context): et utiliser adsk.core/adsk.fusion. "
            "Unités : centimètres (3 cm → createByReal(3))."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["execute_script", "read", "undo", "redo"],
                    "description": "Action Fusion 360 à effectuer",
                },
                "script": {
                    "type": "string",
                    "description": (
                        "Script Python Fusion API complet avec def run(context):"
                        " (requis pour execute_script)"
                    ),
                },
                "query_type": {
                    "type": "string",
                    "enum": ["screenshot", "document", "projects", "apiDocumentation"],
                    "description": "Type de lecture (pour action=read)",
                },
                "direction": {
                    "type": "string",
                    "enum": [
                        "current",
                        "front",
                        "back",
                        "top",
                        "bottom",
                        "left",
                        "right",
                        "iso-top-right",
                    ],
                    "description": "Direction caméra pour screenshot",
                },
                "name": {
                    "type": "string",
                    "description": "Terme de recherche pour query_type=document",
                },
            },
            "required": ["action"],
        },
    },
]


class WorkerAgent:
    def __init__(
        self,
        project: Project,
        store: ProjectStore,
        broadcast_event: Callable[[dict], None],
        approval_callback: Callable[[str, str, str], Awaitable[bool | None]],
        llm: LLMProvider,
        budget_guard: BudgetGuard | None = None,
        governance: Governance | None = None,
        verifier: Verifier | None = None,
        reflexion: Reflexion | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._project = project
        self._store = store
        self._broadcast = broadcast_event
        self._approval_cb = approval_callback
        self._llm = llm
        self._budget = budget_guard
        self._worker_id = uuid.uuid4().hex[:8]  # identifiant unique pour les claims
        self._file_tool = SandboxedFileTool(
            project.workspace_path,
            allow_network=bool(getattr(project, "requires_network", False)),
        )
        self._cli_tool = WorkerCLITool(project.workspace_path)
        self._docker = None
        self._killed = False
        self._quality = QualityChecker(project.workspace_path)
        # Refus de POLITIQUE rencontrés pendant l'étape courante (backend absent,
        # commande hors whitelist, garde de script). Ils portent la vraie cause
        # d'un échec ; sans eux l'utilisateur ne lit que « trop d'étapes ».
        self._blockers: list[str] = []
        # Cause d'un échec qui ne tient à aucune étape (exception, étape restée
        # PENDING) — sans elle l'annonce de fin dirait « échouée » sans raison.
        self._failure_reason: str | None = None
        # PHASE 1 — governance et verifier (injection ou construction tardive).
        self._governance = governance
        self._verifier = verifier
        # PHASE 2 — reflexion post-mission (injection optionnelle).
        # Si None, aucune leçon n'est produite (mode dégradé silencieux).
        # Phase D — la Reflexion canonique est désormais abonnée à
        # `MissionCompleted` via le bus ; on garde le paramètre pour les
        # tests legacy qui appellent _maybe_reflect directement.
        self._reflexion = reflexion
        self._bus = bus

    def kill(self) -> None:
        self._killed = True
        logger.info("WorkerAgent killed", project_id=self._project.id)

    def _ensure_governance(self) -> None:
        """Construit une Governance par défaut si non injectée (singletons globaux)."""
        if self._governance is not None:
            return

        audit_path = Path(self._project.workspace_path) / ".jarvis" / "audit.jsonl"
        self._governance = Governance(
            approval_config=approval_config,
            budget_guard=self._budget,
            audit_log=AuditLog(audit_path),
        )

    def _ensure_verifier(self) -> None:
        """Construit un Verifier par défaut si non injecté (LLM Anthropic Haiku)."""
        if self._verifier is not None:
            return

        self._verifier = Verifier(quality_checker=self._quality, llm=self._llm)

    async def _setup_environment(self) -> None:
        """Configure l'environnement d'exécution : Docker V2 ou direct V1."""

        self._ensure_governance()
        # Le verifier doit utiliser le _cli_tool ACTUEL (potentiellement Dockerisé).
        # On le construit après la sélection du backend pour qu'il pointe sur le bon CLI.

        if settings.docker_enabled:
            available = await DockerExecutor.is_available()
            if not available:
                await self._log("warning", "Docker non disponible — fallback V1 direct")
            else:
                network = "bridge" if self._project.requires_network else settings.docker_network
                self._docker = DockerExecutor(
                    workspace_path=self._project.workspace_path,
                    project_id=self._project.id,
                    network=network,
                )
                await self._docker.start()
                self._cli_tool = WorkerCLITool(
                    workspace_path=self._project.workspace_path,
                    docker_executor=self._docker,
                )
                await self._log(
                    "info", f"Environnement Docker démarré ({settings.docker_base_image})"
                )
        else:
            await self._log("info", "Environnement direct V1")

        # Verifier construit après le choix du backend CLI (peut être Dockerisé).
        # CRITIQUE : doit être appelé sur TOUTES les branches, sinon le verifier reste None
        # et `_execute_with_verification` court-circuite la couche 3.
        self._ensure_verifier()

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        project = self._project
        project.status = ProjectStatus.RUNNING
        project.started_at = datetime.now()
        self._store.save_project(project)
        await self._log("info", f"Démarrage : {project.title}")
        self._push_update()

        try:
            # DANS le try, pas avant : s'il lève (Docker qui ne démarre pas…),
            # l'exception sortait de run() sans passer par `finally`. La mission
            # restait RUNNING indéfiniment, jamais sauvegardée comme échouée, et
            # seule la réconciliation au redémarrage suivant la rattrapait.
            await self._setup_environment()

            for step in project.steps:
                if step.status in (StepStatus.DONE, StepStatus.SKIPPED):
                    continue  # déjà complétée — on ne re-exécute pas
                if self._killed:
                    project.status = ProjectStatus.KILLED
                    break
                await self._execute_step(step)
                if step.status == StepStatus.FAILED:
                    project.status = ProjectStatus.FAILED
                    await self._log("error", f"Étape échouée : {step.title}")
                    break
            else:
                # Une étape sautée (réclamation périmée, par exemple) ne lève pas
                # d'échec : la boucle allait au bout et le projet se déclarait
                # « terminé avec succès » avec une étape jamais exécutée
                # (proj_eb0459 : 3/4, étape 2 en attente). On ne signe pas un
                # succès sur un trou.
                unfinished = [
                    s
                    for s in project.steps
                    if s.status
                    in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.WAITING_APPROVAL)
                ]
                if unfinished:
                    project.status = ProjectStatus.FAILED
                    project.completed_at = datetime.now()
                    titles = ", ".join(s.title for s in unfinished[:3])
                    self._failure_reason = f"étape(s) jamais exécutée(s) : {titles}"
                    await self._log(
                        "error",
                        f"Mission incomplète — {len(unfinished)} étape(s) jamais exécutée(s) : "
                        f"{titles}",
                    )
                    # `finally` sauvegarde et pousse la mise à jour : pas de
                    # double enregistrement ici.
                    return
                # La recette : la DEMANDE est-elle satisfaite ? Seule porte finale.
                verdict = await self._run_acceptance()
                project.completed_at = datetime.now()
                if not verdict.accepted:
                    project.status = ProjectStatus.FAILED
                    manques = "; ".join(verdict.missing[:3])
                    self._failure_reason = f"recette refusée — {verdict.reason}" + (
                        f" ({manques})" if manques else ""
                    )
                    return
                project.status = ProjectStatus.DONE
                await self._log("info", "✓ Projet terminé avec succès")
                self._broadcast(
                    {
                        "type": "project_done",
                        "project_id": project.id,
                        "title": project.title,
                    }
                )
        except Exception as e:
            collector.error("JRV-MSN-001", "JRV-MSN-001", cause=e)
            project.status = ProjectStatus.FAILED
            self._failure_reason = describe_exception(e)
            await self._log("error", f"Erreur inattendue : {self._failure_reason}")
        finally:
            # PHASE 2 §5.1 — réflexion post-mission sur statut terminal.
            # PAUSED est exclu (mission reprenable). Best-effort, jamais bloquant.
            # Phase D : la Reflexion canonique est branchée via le bus
            # (`MissionCompleted` → handler `_on_mission_completed` dans
            # bootstrap). Le call direct `_maybe_reflect` reste pour les tests
            # legacy qui injectent `reflexion=` mais pas `bus=`.
            if self._bus is not None and project.status in (
                ProjectStatus.DONE,
                ProjectStatus.FAILED,
                ProjectStatus.KILLED,
            ):
                await self._bus.publish(
                    MissionCompleted(
                        mission_id=project.id,
                        verdict={
                            ProjectStatus.DONE: "success",
                            ProjectStatus.FAILED: "failure",
                            ProjectStatus.KILLED: "killed",
                        }.get(project.status, "failure"),
                    )
                )
            elif self._reflexion is not None:
                await self._maybe_reflect()

            if self._docker:
                await self._docker.stop()
            self._store.save_project(project)
            self._push_update()
            self._announce_finished()

    async def _maybe_reflect(self) -> None:
        """Appelle Reflexion si terminale + injectée. Dégrade silencieusement sinon."""
        if self._reflexion is None:
            return
        if self._project.status not in (
            ProjectStatus.DONE,
            ProjectStatus.FAILED,
            ProjectStatus.KILLED,
        ):
            return
        try:
            lesson = await self._reflexion.reflect(self._project)
        except Exception as exc:  # noqa: BLE001 — la mission est close, on log et basta
            collector.error("JRV-MSN-001", "JRV-MSN-001", cause=exc)
            logger.warning(
                "Reflexion error in worker.finally",
                project_id=self._project.id,
                error=str(exc),
            )
            return
        if lesson is None:
            return
        await self._log(
            "info",
            f"Leçon produite (skill_candidate={lesson.skill_candidate}) : "
            f"{lesson.corrective_action[:120] or lesson.what_worked[:120]}",
        )
        self._broadcast(
            {
                "type": "mission_lesson_produced",
                "project_id": self._project.id,
                "lesson_event_id": lesson.lesson_event_id,
                "skill_candidate": lesson.skill_candidate,
                "skill_description": lesson.skill_description,
            }
        )

    # ── Step execution ─────────────────────────────────────────────────────────

    async def _execute_step(self, step: Step) -> None:
        # Claim atomique — évite la double-exécution si plusieurs workers tournent
        if not self._store.claim_step(self._project.id, step.id, self._worker_id):
            await self._log(
                "warning",
                f"Étape déjà réclamée par un autre worker : {step.title}",
                step_id=step.id,
            )
            return

        step.status = StepStatus.RUNNING
        step.started_at = datetime.now()
        self._store.save_project(self._project)
        await self._log("info", f"→ {step.title}", step_id=step.id)
        self._push_update()

        # ── PHASE 1 §4.5 — gate composite avant le step ───────────────────────
        gate_decision = await self._gate_step(step)
        if gate_decision == GateDecision.REFUSED:
            step.status = StepStatus.FAILED
            step.error = "Gate composite : REFUSED (catégorie NEVER ou budget hard_stop)"
            await self._log("error", "Gate REFUSED — step bloqué", step_id=step.id)
            self._store.save_project(self._project)
            self._push_update()
            return

        # Approbation : par gate (APPROVAL/DRY_RUN) OU par flag legacy requires_approval (Q1=a).
        gate_wants_approval = gate_decision in (GateDecision.APPROVAL, GateDecision.DRY_RUN)
        if gate_wants_approval or step.requires_approval:
            step.status = StepStatus.WAITING_APPROVAL
            self._store.save_project(self._project)
            self._push_update()
            reason = "Gate composite" if gate_wants_approval else "plan : requires_approval"
            await self._log(
                "approval",
                f"Approbation requise ({reason}) : {step.title}",
                step_id=step.id,
            )

            decision = await self._approval_cb(self._project.id, step.id, step.description)

            if decision is not True:
                step.status = StepStatus.SKIPPED
                if decision is None:
                    # Personne n'a répondu. Ce n'est PAS un refus : la demande
                    # n'a peut-être jamais été affichée. Le dire tel quel évite
                    # d'imputer à l'utilisateur une décision qu'il n'a pas prise.
                    step.output = (
                        "Aucune réponse à la demande d'approbation — "
                        "elle a expiré sans décision."
                    )
                    await self._log(
                        "warning",
                        f"Approbation sans réponse : {step.title}",
                        step_id=step.id,
                    )
                else:
                    step.output = "Refusée par l'utilisateur."
                    await self._log("info", f"Étape refusée : {step.title}", step_id=step.id)
                self._store.save_project(self._project)
                self._push_update()
                return

            step.status = StepStatus.RUNNING
            self._store.save_project(self._project)

        # ── PHASE 1 §4.4 — exécution avec retry borné de la vérification ─────
        await self._execute_with_verification(step)

        self._store.save_project(self._project)
        self._push_update()

    def _is_fusion(self) -> bool:
        """Mission Fusion 360 : le résultat vit dans Fusion, pas dans le workspace."""
        text = f"{self._project.mission} {self._project.title}".lower()
        return "fusion" in text

    async def _execute_with_verification(self, step: Step) -> None:
        """Exécute une étape. Seul un défaut OBJECTIF la fait échouer.

        Une étape est un progrès, pas un verdict. On ne vérifie ici que ce qui doit
        être vrai à tout moment (aucun fichier vide, Python valide) ; savoir si la
        DEMANDE est satisfaite revient à la recette de fin de mission (_accept).
        Juger chaque étape sur son texte faisait mourir des missions correctes.
        """
        issues: list[str] = []
        self._blockers = []

        for attempt in range(_STEP_ATTEMPTS):
            try:
                result = await asyncio.wait_for(
                    self._run_step_llm(step, prev_issues=issues, attempt=attempt),
                    timeout=300,
                )
                step.output = result
            except BudgetExceeded:
                collector.error("JRV-MSN-001", "JRV-MSN-001")
                # Hard-stop budget : on met le projet en pause (reprise possible)
                await self._log(
                    "warning",
                    f"Budget épuisé — pause du projet : {step.title}",
                    step_id=step.id,
                )
                self._store.pause_for_budget(self._project, step.id)
                self._push_update()
                self._broadcast(
                    {
                        "type": "budget_hard_stop",
                        "project_id": self._project.id,
                        "step_id": step.id,
                        "message": (
                            "Budget atteint — projet mis en pause. Reprise possible après recharge."
                        ),
                    }
                )
                return
            except TimeoutError:
                collector.error("JRV-MSN-001", "JRV-MSN-001")
                step.status = StepStatus.FAILED
                step.error = "Timeout (5 min) dépassé."
                await self._log("error", f"Timeout : {step.title}", step_id=step.id)
                return
            except Exception as e:  # noqa: BLE001 — exec failure surfaced as step FAILED
                collector.error("JRV-MSN-001", "JRV-MSN-001", cause=e)
                step.status = StepStatus.FAILED
                step.error = str(e)
                await self._log("error", f"Erreur : {step.title} — {e}", step_id=step.id)
                return

            check = self._verifier.check_step() if self._verifier else None
            if self._is_fusion() or check is None or check.verified:
                step.status = StepStatus.DONE
                step.verified = check is not None and check.verified
                step.completed_at = datetime.now()
                await self._log(
                    "info", f"✓ {step.title}", step_id=step.id, data={"output": result[:300]}
                )
                return

            issues = check.issues
            await self._log(
                "warning",
                f"Défaut objectif, essai {attempt + 1}/{_STEP_ATTEMPTS} : {'; '.join(issues[:3])}",
                step_id=step.id,
            )

        step.status = StepStatus.FAILED
        cause = self._blockers or issues
        step.error = "Défaut non corrigé : " + " | ".join(cause[:3])
        await self._log("error", f"Étape échouée : {step.title}", step_id=step.id)

    async def _gate_step(self, step: Step) -> GateDecision:
        """Appelle le gate composite pour ce step (§4.5)."""
        assert self._governance is not None  # garanti par _ensure_governance
        ctx = GateContext(
            access_level=step.access_level,
            action_category="agent_mission",
            estimated_cost_usd=0.02,  # estimation conservatrice (cf. _run_step_llm)
            budget_scope=f"project:{self._project.id}",
            description=f"step:{step.title}",
        )
        return self._governance.gate(ctx, f"step:{self._project.id}:{step.id}")

    # ── LLM tool-loop ─────────────────────────────────────────────────────────

    async def _run_step_llm(
        self,
        step: Step,
        prev_issues: list[str] | None = None,
        attempt: int = 0,
    ) -> str:

        # Vérification budget avant l'appel LLM (estimation conservatrice : 0.02 USD / step)
        _est_usd = 0.02
        if self._budget is not None:
            global_ok = await self._budget.reserve("global", _est_usd)
            project_ok = await self._budget.reserve(f"project:{self._project.id}", _est_usd)
            if not global_ok or not project_ok:
                raise BudgetExceeded(
                    "JRV-BGT-001",
                    f"Budget dépassé (global={'ok' if global_ok else 'stop'}, "
                    f"project={'ok' if project_ok else 'stop'})",
                )

        # Haiku pour le worker : 20x moins cher que Sonnet, largement suffisant.
        # Le LLM est injecté en constructeur (bootstrap utilise voice_llm pour ça).
        llm = self._llm
        self._project.llm_calls += 1

        existing = self._file_tool.list_files()
        context = (
            f"Titre : {self._project.title}\n"
            f"Mission : {self._project.mission}\n"
            # Sans la date, le worker n'a aucun moyen de la connaître : le 23/09 il a
            # écrit la chaîne « $(date +%Y-%m-%d) » dans le fichier, littéralement.
            f"Date du jour : {datetime.now():%Y-%m-%d}\n"
            f"Heure locale : {datetime.now():%H:%M}\n"
            f"Fichiers existants : {', '.join(existing[:15]) or '(aucun)'}"
        )

        system = _WORKER_SYSTEM.format(context=context)
        prompt = (
            f"Étape à exécuter : {step.title}\n\n"
            f"Description : {step.description}\n\n"
            f"Critère de succès à atteindre : {step.success_criterion}\n\n"
            f"Exécute cette étape avec les outils disponibles et retourne un résumé concis."
        )

        # Seconde chance : les défauts OBJECTIFS constatés au premier essai.
        if attempt > 0 and prev_issues:
            issues_text = "\n".join(f"  • {i}" for i in prev_issues[:5])
            prompt += f"\n\nL'essai précédent a laissé ces défauts, à corriger :\n{issues_text}"

        if _QUALITY_RULES:
            system += f"\n\n{_QUALITY_RULES}"

        result = await llm.tool_loop(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            tools=_WORKER_TOOLS,
            tool_executor=self._tool_executor,
            context=f"mission:{self._project.id}",
        )

        # Track files created
        self._project.files_created = self._file_tool.list_files()
        return result

    # ── Tool executor ─────────────────────────────────────────────────────────

    async def _tool_executor(self, name: str, inputs: dict) -> str:
        # PHASE 1 §9 / Q3=c — gate au niveau outil.
        # Chaque tool a son AccessLevel et sa catégorie ; refusé/approbation → court-circuit.
        refusal = await self._gate_tool(name, inputs)
        if refusal is not None:
            return refusal

        try:
            if name == "read_file":
                content = self._file_tool.read_file(inputs["path"])
                await self._log(
                    "tool", f"read_file: {inputs['path']}", data={"chars": len(content)}
                )
                return content

            if name == "write_file":
                result = self._file_tool.write_file(inputs["path"], inputs["content"])
                await self._log(
                    "tool", f"write_file: {inputs['path']}", data={"chars": len(inputs["content"])}
                )
                return result

            if name == "list_files":
                files = self._file_tool.list_files(inputs.get("directory", "."))
                await self._log(
                    "tool",
                    f"list_files: {inputs.get('directory', '.')}",
                    data={"count": len(files)},
                )
                return json.dumps(files)

            if name == "delete_file":
                # Le gate ci-dessus demande l'approbation — s'il a tourné. Sans
                # gouvernance, rien ne l'aurait demandé : on refuse plutôt que de
                # supprimer sans accord.
                if self._governance is None:
                    raise ValueError("suppression impossible sans contrôle d'approbation actif")
                result = self._file_tool.delete_file(inputs["path"])
                await self._log("tool", f"delete_file: {inputs['path']}")
                return result

            if name == "create_directory":
                result = self._file_tool.create_directory(inputs["path"])
                await self._log("tool", f"create_directory: {inputs['path']}")
                return result

            if name == "execute_cli":
                cmd = inputs["command"]
                timeout = int(inputs.get("timeout", 60))
                res = await self._cli_tool.execute(cmd, timeout=timeout)
                # Consigné APRÈS exécution, avec code et sortie : c'est la preuve
                # la plus solide qu'un programme a tourné, et la recette la lit.
                await self._log(
                    "tool",
                    f"execute_cli: {cmd[:120]}",
                    data={
                        "returncode": res.get("returncode"),
                        "output": ((res.get("stdout") or "") + (res.get("stderr") or ""))[:600],
                    },
                )
                if res["success"]:
                    return res["stdout"] or "(commande exécutée, pas de sortie)"
                if res.get("blocked"):
                    # Refus de politique : aucun retry ne peut le lever. On le
                    # mémorise pour le rapport ET on le dit au modèle, sinon il
                    # relance la même commande jusqu'à épuiser sa boucle d'outils.
                    self._note_blocker(res["stderr"])
                    return (
                        f"REFUS DÉFINITIF : {res['stderr']} "
                        f"Inutile de réessayer cette commande — elle restera refusée "
                        f"tant que la configuration n'aura pas changé. Termine l'étape "
                        f"en expliquant ce blocage."
                    )
                return f"ERREUR (rc={res['returncode']}) : {res['stderr']}"

            if name == "fusion_360":
                from jarvis.capabilities.tools.fusion import FusionTool  # lazy: plugin Fusion

                action = inputs.get("action", "")
                await self._log("tool", f"fusion_360: {action}", data={"inputs": str(inputs)[:120]})
                tool = FusionTool()
                result = await tool.execute(**inputs)
                if result.is_error:
                    await self._log("error", f"fusion_360 erreur: {result.content[:200]}")
                return result.content

            return f"Outil inconnu : {name}"

        except ValueError as e:
            collector.error("JRV-MSN-001", "JRV-MSN-001", cause=e)
            # Sandbox violation ou garde de contenu : politique, pas incident.
            await self._log("error", f"SANDBOX: {e}")
            self._note_blocker(str(e))
            return f"ACCÈS REFUSÉ : {e}"
        except Exception as e:
            collector.error("JRV-MSN-001", "JRV-MSN-001", cause=e)
            await self._log("error", f"Tool error {name}: {e}")
            return f"Erreur : {e}"

    def mark_timed_out(self, minutes: int) -> None:
        """Clôt la mission quand l'orchestrateur a dépassé son délai.

        `asyncio.wait_for` ANNULE run() à l'expiration. CancelledError n'est pas
        une Exception : le `except` de run() ne la voyait pas, et `finally`
        sauvegardait la mission telle quelle — en RUNNING. Elle y restait
        jusqu'au prochain redémarrage, sans aucune annonce. Appelé par
        l'orchestrateur une fois l'annulation terminée.
        """
        project = self._project
        if project.status in (ProjectStatus.DONE, ProjectStatus.FAILED, ProjectStatus.KILLED):
            return
        cause = f"délai de la mission dépassé ({minutes} min)"
        for step in project.steps:
            if step.status in (StepStatus.RUNNING, StepStatus.WAITING_APPROVAL):
                step.status = StepStatus.FAILED
                step.error = cause
        project.status = ProjectStatus.FAILED
        project.completed_at = datetime.now()
        self._failure_reason = cause
        self._store.save_project(project)
        self._push_update()
        self._announce_finished()

    async def _run_acceptance(self) -> Acceptance:
        """Recette de fin de mission, avec UNE ronde de correction au besoin.

        Le juge voit la demande, chaque fichier et son contenu, et le journal des
        actions (écritures, suppressions, commandes et leurs sorties). S'il refuse
        preuve à l'appui, une étape « Corriger d'après la recette » — visible dans
        le dashboard — reçoit ses manques précis, puis la recette est rejouée une
        fois. Un Retry refait une recette (et au besoin une nouvelle correction).
        """
        if self._is_fusion() or self._verifier is None:
            return Acceptance(accepted=True, reason="recette non applicable")

        await self._log("info", "Recette : la demande est-elle satisfaite ?")
        verdict = await self._verifier.accept(self._project, self._store.get_logs(self._project))
        if verdict.accepted or not verdict.judged:
            await self._log("info" if verdict.accepted else "error", f"Recette : {verdict.reason}")
            return verdict

        await self._log(
            "warning", f"Recette refusée : {verdict.reason}", data={"missing": verdict.missing}
        )
        repair = self._repair_step(verdict)
        self._project.steps.append(repair)
        self._store.save_project(self._project)
        self._push_update()
        await self._execute_step(repair)
        if repair.status is not StepStatus.DONE:
            return Acceptance(accepted=False, reason=repair.error or verdict.reason)

        verdict = await self._verifier.accept(self._project, self._store.get_logs(self._project))
        await self._log("info" if verdict.accepted else "error", f"Recette : {verdict.reason}")
        return verdict

    def _repair_step(self, verdict: Acceptance) -> Step:
        """Étape de correction ciblée sur les manques CITÉS par la recette."""
        manques = "; ".join(verdict.missing) or verdict.reason
        return Step(
            id=f"step_{len(self._project.steps) + 1:03d}",
            title="Corriger d'après la recette",
            description=(
                f"La vérification finale a refusé la mission : {verdict.reason} "
                f"Corrige précisément ces manques : {manques}. "
                f"Ne refais pas ce qui est déjà correct."
            ),
            success_criterion="Les manques signalés par la recette sont corrigés.",
        )

    def _announce_finished(self) -> None:
        """Annonce la fin de mission — succès, échec ou arrêt — une seule fois.

        `project_done` n'était émis qu'en cas de SUCCÈS : une mission échouée ne
        produisait aucun événement terminal propre, seulement un project_update
        générique. Personne ne pouvait dire « ta mission a échoué » sans deviner.

        Deux événements, depuis ce seul point de `finally` :
        - `project_finished` (structuré) pour qui veut réagir à la fin, quel que
          soit le statut — c'est le point d'extension à utiliser ;
        - un message sur le canal passif, que la page d'accueil affiche déjà.
        `project_done` reste émis au succès, pour les écouteurs existants.
        """
        project = self._project
        if project.status not in (
            ProjectStatus.DONE,
            ProjectStatus.FAILED,
            ProjectStatus.KILLED,
        ):
            return  # PAUSED, ou run() interrompu : pas une fin.
        self._broadcast(
            {
                "type": "project_finished",
                "project_id": project.id,
                "title": project.title,
                "status": str(project.status),
                "files": list(project.files_created),
                "reason": self._failure_reason,
            }
        )
        self._broadcast(
            mission_announcements.chat_message(
                mission_announcements.finished(project, reason=self._failure_reason)
            )
        )

    def _note_blocker(self, message: str) -> None:
        """Mémorise un refus de politique, sans doublon, pour le rapport d'échec."""
        cleaned = " ".join(message.split())[:300]
        if cleaned and cleaned not in self._blockers:
            self._blockers.append(cleaned)

    async def _gate_tool(self, name: str, inputs: dict) -> str | None:
        """Gate au niveau outil (Q3=c). Renvoie un message de refus, ou None si autorisé."""
        if self._governance is None:
            return None
        al = _TOOL_ACCESS_LEVEL.get(name, AccessLevel.WRITE_LOCAL)
        cat = _TOOL_CATEGORY.get(name, "agent_mission")
        ctx = GateContext(
            access_level=al,
            action_category=cat,
            estimated_cost_usd=0.0,
            budget_scope=f"project:{self._project.id}",
            description=f"{name} {json.dumps(inputs)[:200]}",
        )
        decision = self._governance.gate(
            ctx, f"tool:{name}:{self._project.id}:{uuid.uuid4().hex[:6]}"
        )
        if decision == GateDecision.AUTO:
            return None
        if decision == GateDecision.REFUSED:
            await self._log("error", f"Tool REFUSED par gate : {name} (cat. {cat})")
            return (
                f"ACCÈS REFUSÉ : action '{name}' (cat. {cat}, niveau {int(al)}) "
                f"bloquée par configuration utilisateur (catégorie NEVER ou budget hard_stop)."
            )
        # APPROVAL ou DRY_RUN → demander à l'humain
        approval_id = f"tool-{uuid.uuid4().hex[:6]}"
        decision = await self._approval_cb(
            self._project.id,
            approval_id,
            f"Outil '{name}' (cat. {cat}, niveau {int(al)}) requiert votre approbation",
        )
        if decision is not True:
            if decision is None:
                await self._log(
                    "warning",
                    f"Tool {name} : approbation sans réponse (demande expirée)",
                    data={"category": cat},
                )
                return (
                    f"ACCÈS REFUSÉ : aucune réponse à la demande d'approbation "
                    f"pour '{name}' — elle a expiré sans décision."
                )
            await self._log(
                "warning", f"Tool {name} non approuvé par l'utilisateur", data={"category": cat}
            )
            return f"ACCÈS REFUSÉ : approbation utilisateur refusée pour '{name}'."
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _log(
        self,
        level: str,
        message: str,
        step_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        entry = LogEntry(
            timestamp=datetime.now(),
            level=level,
            message=message,
            step_id=step_id,
            data=data,
        )
        self._store.append_log(self._project, entry)
        logger.debug("WorkerAgent log", level=level, msg=message[:80])

    def _push_update(self) -> None:
        self._broadcast(
            {
                "type": "project_update",
                "project_id": self._project.id,
                "status": self._project.status,
                "steps": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "status": s.status,
                        "requires_approval": s.requires_approval,
                        "output": s.output,
                        "error": s.error,
                    }
                    for s in self._project.steps
                ],
            }
        )
