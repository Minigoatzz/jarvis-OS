# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Verification d'une mission : un controle objectif par etape, une recette a la fin.

Pourquoi cette forme
--------------------
L'ancienne version jugeait CHAQUE etape comme une porte bloquante, avec trois
couches : la structure, une commande shell ecrite par le planificateur, puis un
juge LLM. Sur l'ensemble des missions : 15 refus, dont 13 venaient des deux
dernieres couches — et les deux missions mortes du 28/09 avaient un travail
correct (proj_b67093) ou corrige juste avant le refus (proj_e7b565).

- La commande du planificateur jugeait le TEXTE du code, pas le but : elle a
  refuse un script juste et valide une suppression jamais faite.
- Le juge LLM ne voyait que les fichiers nouveaux — ni les modifies, ni les
  supprimes — et avait pour consigne « dans le doute, refuse ».
- Chaque porte devait passer : 8 portes a 90 % donnent 43 % de reussite.

Chaque correctif ajoute ensuite (evaluateur natif de commandes shell, filtre
a tautologies, messages de relance) compensait le precedent. Ils sont retires.

Ce qui reste
------------
1. `check_step` — par etape, uniquement ce qui est vrai a TOUT moment de la
   mission : aucun fichier vide, Python syntaxiquement valide. Sur tous les
   fichiers, pas seulement les nouveaux.
2. `accept` — une seule recette, a la fin, sur la DEMANDE de l'utilisateur, avec
   toutes les preuves : chaque fichier et son contenu, le journal des actions
   (ecritures, suppressions, commandes et leurs sorties). Pour refuser, le juge
   doit citer ce qui manque. Sans preuve de manque, la mission est acceptee.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from jarvis.engine.mission.quality_checker import QualityChecker
from jarvis.engine.mission.schemas import Project
from jarvis.kernel.contracts import LLMProvider
from jarvis.kernel.error_collector import collector  # jrv: autofix
from jarvis.kernel.schemas import LogEntry

# Budget de preuves transmis au juge (caracteres / fichiers / lignes de journal).
_MAX_CONTENT_CHARS = 6000
_MAX_FILES = 12
_MAX_JOURNAL = 40

# Niveaux du journal qui constituent une PREUVE d'action.
_EVIDENCE_LEVELS = ("tool", "error", "approval")

_TEXT_EXTS = {
    ".html", ".css", ".js", ".ts", ".py", ".json", ".md", ".txt",
    ".yaml", ".yml", ".toml", ".xml", ".sh", ".csv",
}


@dataclass
class VerificationResult:
    """Verdict du controle objectif d'une etape."""

    verified: bool
    issues: list[str] = field(default_factory=list)


@dataclass
class Acceptance:
    """Verdict de la recette finale."""

    accepted: bool
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    # False quand le juge n'a PAS PU se prononcer (erreur, réponse illisible) :
    # il n'y a alors rien de précis à corriger, donc pas de ronde de correction.
    judged: bool = True


_ACCEPT_SYSTEM = (
    "Tu es le responsable de la recette d'une mission confiée à un agent. Tu juges si "
    "la DEMANDE de l'utilisateur est satisfaite par l'état final. Tu réponds UNIQUEMENT "
    "en JSON, sans markdown ni préambule."
)


class Verifier:
    """Controle objectif par etape + recette finale fondee sur les preuves."""

    def __init__(self, quality_checker: QualityChecker, llm: LLMProvider) -> None:
        self._quality = quality_checker
        self._llm = llm

    # ── Par etape : objectif, sans LLM ────────────────────────────────────────

    def check_step(self) -> VerificationResult:
        issues = self._quality.check_step_output()
        return VerificationResult(verified=not issues, issues=issues)

    # ── Fin de mission : la recette ───────────────────────────────────────────

    async def accept(self, project: Project, journal: list[LogEntry]) -> Acceptance:
        prompt = (
            f"## Demande de l'utilisateur\n{project.mission}\n\n"
            f"## Étapes réalisées (auto-rapport de l'agent — pas une preuve)\n"
            f"{self._steps_summary(project)}\n\n"
            f"## Journal des actions réellement effectuées (preuve)\n"
            f"{self._journal_block(journal)}\n\n"
            f"## Fichiers présents à la fin, avec leur contenu (preuve)\n"
            f"{self._files_block(project.workspace_path)}\n\n"
            f"## Contrôle qualité automatique\n{self._quality_block()}\n\n"
            "## Ta décision\n"
            "Juge la DEMANDE, pas la manière. Un programme correct écrit autrement que tu "
            "l'aurais fait est correct. Un fichier absent parce que la demande disait de le "
            "supprimer est un succès. Accepte si la demande est satisfaite. Refuse "
            "SEULEMENT en citant ce qui manque ou est faux, preuve à l'appui (nom de "
            "fichier, contenu, sortie de commande). Sans preuve d'un manque, accepte.\n"
            "Réponds avec ce JSON exactement :\n"
            '{"accepted": true|false, "missing": ["..."], "reason": "une phrase"}\n'
        )
        for attempt in (1, 2):  # une seconde chance si la reponse est illisible
            try:
                raw = await self._llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    system=_ACCEPT_SYSTEM,
                    stream=False,
                    context="mission-acceptance",
                )
            except Exception as exc:  # noqa: BLE001 — l'erreur LLM est rapportee telle quelle
                collector.error("JRV-MSN-001", "JRV-MSN-001", cause=exc)
                return Acceptance(
                    accepted=False, reason=f"recette impossible : {exc}", judged=False
                )
            verdict = _parse_json(raw if isinstance(raw, str) else "")
            if verdict is not None:
                missing = [str(m) for m in (verdict.get("missing") or [])][:10]
                return Acceptance(
                    accepted=verdict.get("accepted") is True,
                    reason=" ".join(str(verdict.get("reason") or "").split())[:300],
                    missing=missing,
                )
            logger.warning(f"Recette : réponse illisible (essai {attempt}/2)")
        return Acceptance(
            accepted=False, reason="recette impossible : réponse du juge illisible", judged=False
        )

    # ── Preuves ───────────────────────────────────────────────────────────────

    @staticmethod
    def _steps_summary(project: Project) -> str:
        lines = []
        for s in project.steps:
            output = " ".join(str(s.output or "").split())[:160]
            lines.append(f"- [{s.status}] {s.title} — {output}")
        return "\n".join(lines) or "(aucune étape)"

    @staticmethod
    def _journal_block(journal: list[LogEntry]) -> str:
        """Actions reelles, dont les SUPPRESSIONS et les SORTIES de commandes.

        L'ancien juge ne voyait que les fichiers nouveaux : une suppression reussie
        etait indiscernable d'un oubli (proj_e7b565).
        """
        rows = []
        for entry in journal:
            if entry.level not in _EVIDENCE_LEVELS:
                continue
            line = f"- [{entry.level}] {entry.message}"
            data = entry.data if isinstance(entry.data, dict) else {}
            if "returncode" in data:
                sortie = " ".join(str(data.get("output") or "").split())[:300]
                line += f" → code {data['returncode']}" + (f", sortie : {sortie}" if sortie else "")
            rows.append(line)
        return "\n".join(rows[-_MAX_JOURNAL:]) or "(aucune action enregistrée)"

    def _files_block(self, workspace_path: str) -> str:
        files = self._quality.list_all_files()
        if not files:
            return "(workspace vide)"
        ws = Path(workspace_path).resolve()
        parts, used = [], 0
        for f in files[:_MAX_FILES]:
            path = f["path"]
            if Path(path).suffix.lower() not in _TEXT_EXTS:
                parts.append(f"=== {path} ({f['size']} o, binaire) ===")
                continue
            try:
                content = (ws / path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                collector.error("JRV-MSN-001", "JRV-MSN-001", cause=exc)
                parts.append(f"=== {path} (illisible : {exc}) ===")
                continue
            room = max(0, _MAX_CONTENT_CHARS - used)
            parts.append(f"=== {path} ({f['size']} o) ===\n{content[:room]}")
            used += min(len(content), room)
        if len(files) > _MAX_FILES:
            parts.append(f"(+{len(files) - _MAX_FILES} autres fichiers)")
        return "\n".join(parts)

    def _quality_block(self) -> str:
        report = self._quality.generate_report()
        issues = report.get("issues") or []
        return "\n".join(f"- {i}" for i in issues[:10]) or "(aucun problème détecté)"


def _parse_json(raw: str) -> dict | None:
    """Extrait l'objet JSON de la reponse, meme entoure d'une cloture markdown."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if not 0 <= start < end:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        collector.error("JRV-MSN-001", "JRV-MSN-001")
        return None
    return parsed if isinstance(parsed, dict) else None
