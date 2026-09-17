# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — tout outil promis au modèle doit exister dans le registre.

Bug réel : `map_control` est implémenté (capabilities/tools/map_control.py) et
documenté sur six lignes du prompt statique — « Montre-moi Lyon » →
`map_control(action="fly_to", location="lyon", zoom=11)` — mais n'était jamais
passé à `tool_registry.register()`. Conséquences en chaîne :

  - le modèle lit le prompt, écrit consciencieusement l'appel ;
  - `Agent.extract_text_tool_calls` ne reconnaît que les noms RÉELLEMENT
    enregistrés (garde-fou voulu : rien d'autre ne doit pouvoir s'exécuter) ;
  - la ligne est donc ignorée, sans erreur, sans log ;
  - la skill-vue `globe` s'affiche mais reste impilotable, et l'utilisateur
    conclut que « les skills installés ne marchent pas ».

Aucun test n'aurait attrapé ça : l'outil est correct, le prompt est correct,
c'est le CÂBLAGE entre les deux qui manquait. Ce fichier teste ce câblage.

On lit bootstrap.py en AST plutôt que d'appeler `build()` : la construction
réelle exige des credentials, des chemins et un LLM joignable, alors que la
question posée ici est purement structurelle.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "src" / "jarvis" / "bootstrap.py"
_TOOLS_DIR = _ROOT / "src" / "jarvis" / "capabilities" / "tools"
_STATIC_PROMPT = _ROOT / "prompts" / "system_static.md"

# Outils implémentés mais volontairement NON câblés : ils pilotent du matériel
# absent de la machine (Fusion 360 lancé, imprimante BambuLab joignable) et
# n'ont rien à faire dans le menu d'outils tant que ce matériel n'est pas là.
# Pour les activer : les importer dans bootstrap.py et les ajouter au
# `tool_registry.register(...)`, comme MapControlTool.
_DELIBERATELY_UNWIRED = {"fusion_360", "printer_3d"}


def _registered_class_names() -> set[str]:
    """Classes d'outils réellement passées à `tool_registry.register(...)`.

    bootstrap.py enregistre de deux façons : l'instance directement
    (`register(WeatherTool(), ...)`) ou une variable construite plus haut
    (`calendar_list_tool = CalendarListTool(...)` puis
    `register(..., calendar_list_tool)`). On résout donc les affectations locales avant de
    conclure, plutôt que de deviner le nom de la variable.
    """
    tree = ast.parse(_BOOTSTRAP.read_text(encoding="utf-8"))

    var_to_class: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            callee = node.value.func
            if isinstance(callee, ast.Name):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        var_to_class[target.id] = callee.id

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "register"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                names.add(arg.func.id)
            elif isinstance(arg, ast.Name):
                names.add(var_to_class.get(arg.id, arg.id))
    return names


def _defined_tools() -> dict[str, str]:
    """{nom d'outil: nom de classe} pour toute classe `X(Tool)` de capabilities."""
    found: dict[str, str] = {}
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"class (\w+)\(Tool\):(.*?)(?=\nclass |\Z)", text, re.S):
            cls, body = match.group(1), match.group(2)
            name = re.search(r'name = "([^"]+)"', body)
            if name:
                found[name.group(1)] = cls
    return found


def _is_wired(_tool_name: str, cls: str, registered: set[str]) -> bool:
    """Vrai si la classe de l'outil atteint `register()`, directement ou via
    une variable (résolue par `_registered_class_names`)."""
    return cls in registered


def test_map_control_is_registered() -> None:
    """La régression exacte : sans lui, la vue globe ne répond à rien."""
    registered = _registered_class_names()

    assert "MapControlTool" in registered


@pytest.mark.parametrize(
    "tool_name",
    sorted(set(_defined_tools()) - _DELIBERATELY_UNWIRED),
)
def test_every_defined_tool_is_registered(tool_name: str) -> None:
    """Un outil implémenté et non câblé est invisible ET silencieux."""
    defined = _defined_tools()
    registered = _registered_class_names()

    assert _is_wired(tool_name, defined[tool_name], registered), (
        f"{tool_name} ({defined[tool_name]}) n'est jamais passé à "
        "tool_registry.register() — le modèle ne pourra pas l'appeler. "
        "Câble-le dans bootstrap.py, ou ajoute-le à _DELIBERATELY_UNWIRED "
        "avec la raison."
    )


def test_tools_documented_in_the_static_prompt_are_registered() -> None:
    """Le prompt ne doit rien promettre que le registre ne fournit pas.

    C'est la direction qui a mordu : le prompt enseigne `map_control` par
    l'exemple, donc le modèle l'appelle — et il n'existait nulle part.
    """
    prompt = _STATIC_PROMPT.read_text(encoding="utf-8")
    defined = _defined_tools()
    registered = _registered_class_names()

    documented = {name for name in defined if re.search(rf"\b{re.escape(name)}\(", prompt)}
    assert documented, "aucun outil détecté dans le prompt — le motif a changé ?"

    missing = sorted(
        name
        for name in documented - _DELIBERATELY_UNWIRED
        if not _is_wired(name, defined[name], registered)
    )
    assert not missing, (
        f"Outils enseignés par le prompt statique mais absents du registre : {missing}. "
        "Le modèle écrira l'appel et rien ne s'exécutera, sans la moindre erreur."
    )


def test_unwired_hardware_tools_stay_documented_as_such() -> None:
    """Garde-fou sur la liste d'exceptions : elle doit rester justifiée et réelle."""
    defined = _defined_tools()

    for name in _DELIBERATELY_UNWIRED:
        assert name in defined, (
            f"{name} est listé comme non câblé mais n'existe plus — nettoie la liste."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
