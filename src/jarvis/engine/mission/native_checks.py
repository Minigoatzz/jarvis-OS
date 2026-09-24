# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Vérifications d'étape évaluées EN PYTHON, sans lancer aucun processus.

Panne du 23/09 : la mission « crée un fichier bonjour.txt » a écrit le fichier
correctement puis a échoué, parce que la couche déterministe du Verifier lance
`step.verification_command` et que l'exécution est refusée sur cette machine
(ni Docker, ni ALLOW_UNSANDBOXED_EXEC) : `rc=-1` deux fois, étape FAILED,
mission FAILED. Les deux missions du 13/09 étaient mortes pareil.

Deuxième couche du problème : les commandes générées sont du shell Unix
(`test -f`, `grep -E`, `awk`) alors que la machine est sous Windows, où rien de
tout ça n'existe dans cmd.exe. Même autorisée, l'exécution aurait échoué.

D'où ce module : les vérifications courantes portent sur des fichiers du
workspace (existe / non vide / contient tel motif / a N sections). Python sait
répondre à tout ça directement, sans processus, sans Docker, sans dépendre de
l'OS — et sans donner au modèle le moindre pouvoir d'exécution.

Ce qui n'est pas reconnu retourne None : l'appelant traitera l'étape comme
NON VÉRIFIÉE, jamais comme fausse. Une vérification impossible n'est pas un
échec de l'étape, et le dire autrement est précisément le mensonge qu'on
traque partout ailleurs dans ce projet.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NativeVerdict:
    """Résultat d'une vérification native. `passed` False = critère non atteint."""

    passed: bool
    detail: str


# `grep -c MOTIF FICHIER | awk '{exit ($1 >= N) ? 0 : 1}'` — la forme que le
# planificateur produit pour « au moins N sections ».
_GREP_COUNT_AWK = re.compile(
    r"^grep\s+-c\s+(?P<pat>'[^']*'|\"[^\"]*\"|\S+)\s+(?P<file>\S+)\s*\|\s*awk\s*"
    r"['\"]?\{\s*exit\s*\(\s*\$1\s*>=\s*(?P<n>\d+)\s*\)\s*\?\s*0\s*:\s*1\s*\}['\"]?$"
)


def _resolve(workspace: Path, raw: str) -> Path | None:
    """Chemin DANS le workspace, ou None si la cible tente d'en sortir."""
    candidate = (workspace / raw).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        # jrv: la cible sort du workspace — on refuse d'évaluer, l'appelant
        # marquera l'étape non vérifiée. Cas nominal, volontairement non mappé
        # (scripts/error_audit/scan.py).
        return None
    return candidate


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # jrv: fichier absent ou illisible — c'est une réponse, pas une panne.
        # Volontairement non mappé (scripts/error_audit/scan.py).
        return None


def _test_flag(flag: str, target: Path) -> NativeVerdict | None:
    if flag in ("-f", "-e"):
        ok = target.is_file() if flag == "-f" else target.exists()
        return NativeVerdict(ok, f"{target.name} {'existe' if ok else 'est absent'}")
    if flag == "-d":
        ok = target.is_dir()
        return NativeVerdict(ok, f"dossier {target.name} {'existe' if ok else 'est absent'}")
    if flag == "-s":
        ok = target.is_file() and target.stat().st_size > 0
        return NativeVerdict(ok, f"{target.name} {'est non vide' if ok else 'est vide ou absent'}")
    return None


def _grep(pattern: str, target: Path, *, count_at_least: int | None = None) -> NativeVerdict | None:
    content = _read(target)
    if content is None:
        return NativeVerdict(False, f"{target.name} illisible ou absent")
    try:
        # `\d` et compagnie : GNU grep -E ne les connaît pas, Python si. On est
        # plus permissif que la commande d'origine, jamais plus strict.
        regex = re.compile(pattern, re.MULTILINE)
    except re.error:
        # jrv: motif non traduisible en regex Python — on rend la main plutôt
        # que de deviner. Volontairement non mappé (scripts/error_audit/scan.py).
        return None
    hits = sum(1 for line in content.splitlines() if regex.search(line))
    if count_at_least is not None:
        return NativeVerdict(
            hits >= count_at_least, f"{hits} ligne(s) correspondent, {count_at_least} attendue(s)"
        )
    return NativeVerdict(hits > 0, f"{hits} ligne(s) correspondent au motif")


def _single(command: str, workspace: Path) -> NativeVerdict | None:
    match = _GREP_COUNT_AWK.match(command.strip())
    if match:
        target = _resolve(workspace, match.group("file"))
        if target is None:
            return None
        pattern = match.group("pat").strip("'\"")
        return _grep(pattern, target, count_at_least=int(match.group("n")))

    try:
        parts = shlex.split(command)
    except ValueError:
        # jrv: guillemets déséquilibrés — forme non reconnue, pas une panne.
        # Volontairement non mappé (scripts/error_audit/scan.py).
        return None
    if not parts:
        return None

    # `[ -f x ]` est la même chose que `test -f x`
    if parts[0] == "[" and parts[-1] == "]":
        parts = ["test", *parts[1:-1]]

    head, args = parts[0], parts[1:]

    if head == "test" and len(args) == 2:
        target = _resolve(workspace, args[1])
        return None if target is None else _test_flag(args[0], target)

    if head == "grep":
        flags = [a for a in args if a.startswith("-")]
        operands = [a for a in args if not a.startswith("-")]
        if any(f not in ("-E", "-q", "-i", "-c", "-e") for f in flags) or len(operands) != 2:
            return None
        pattern, filename = operands
        target = _resolve(workspace, filename)
        if target is None:
            return None
        verdict = _grep(pattern, target)
        if verdict is None:
            return None
        if "-i" in flags:  # relu en insensible à la casse
            content = _read(target) or ""
            try:
                hits = len(re.findall(pattern, content, re.IGNORECASE | re.MULTILINE))
            except re.error:
                # jrv: idem, motif illisible — forme non reconnue.
                # Volontairement non mappé (scripts/error_audit/scan.py).
                return None
            return NativeVerdict(hits > 0, f"{hits} correspondance(s), casse ignorée")
        return verdict

    if head in ("cat", "ls") and len(args) == 1:
        target = _resolve(workspace, args[0])
        if target is None:
            return None
        ok = target.exists()
        return NativeVerdict(ok, f"{target.name} {'existe' if ok else 'est absent'}")

    return None


def evaluate(command: str, workspace: Path) -> NativeVerdict | None:
    """Évalue `command` sans processus. None = forme non reconnue.

    Seul `&&` est accepté comme enchaînement : toutes les parties doivent
    passer, comme le ferait le shell. Un `||`, un `;` ou un pipe non reconnu
    rend la main (None) plutôt que de deviner.
    """
    command = command.strip()
    if not command or "||" in command or ";" in command:
        return None

    parts = [p.strip() for p in re.split(r"&&", command)] if "&&" in command else [command]
    details: list[str] = []
    for part in parts:
        verdict = _single(part, workspace)
        if verdict is None:
            return None
        details.append(verdict.detail)
        if not verdict.passed:
            return NativeVerdict(False, " ; ".join(details))
    return NativeVerdict(True, " ; ".join(details))
