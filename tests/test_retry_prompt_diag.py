# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests — `build_retry_prompt` et le script qui le mesure.

Le diagnostic n'a de valeur que s'il mesure EXACTEMENT ce que Jarvis envoie,
et que son barème compte comme échecs les échecs réellement observés le 21/09.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from jarvis.capabilities.tools.show_view import ShowViewTool, _resolve_view_id
from jarvis.engine.agent import build_retry_prompt

_ROOT = Path(__file__).resolve().parents[1]


def _diag():
    spec = importlib.util.spec_from_file_location(
        "diag_retry_prompt", _ROOT / "scripts" / "diag_retry_prompt.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _schemas() -> list[dict]:
    return [ShowViewTool(broadcast_event=lambda e: None).to_claude_schema()]


# ── Le prompt ───────────────────────────────────────────────────────────────


def test_production_prompt_carries_the_place_rules() -> None:
    """Mesuré le 21/09 sur qwen3:14b : 26/36 sans la règle, 36/36 avec."""
    assert "Jamais un lieu" in build_retry_prompt(_schemas())


def test_place_rules_can_still_be_switched_off_for_the_diagnostic() -> None:
    assert "Règles" not in build_retry_prompt(_schemas(), place_rules=False)


def test_place_rules_variant_adds_the_rule_and_nothing_copyable() -> None:
    prompt = build_retry_prompt(_schemas(), place_rules=True)

    assert "action=fly_to" in prompt
    assert "Jamais un lieu" in prompt
    literals = set(re.findall(r'="([^"]*)"', prompt))
    assert literals <= {"valeur"}, f"littéraux copiables : {literals - {'valeur'}}"


def test_force_tool_call_uses_the_shared_builder() -> None:
    """Sinon le diagnostic mesurerait une copie qui peut diverger."""
    import inspect

    from jarvis.engine.agent import Agent

    assert "build_retry_prompt(" in inspect.getsource(Agent.force_tool_call)


# ── Le barème du diagnostic ─────────────────────────────────────────────────


def test_the_real_failures_of_21_09_are_scored_as_failures() -> None:
    diag = _diag()
    assert not diag.score_place([], "paris")
    assert not diag.score_place([("x", "show_view", {"action": "show", "view_id": "paris"})], "paris")
    assert not diag.score_place([("x", "show_view", {"action": "show"})], "paris")
    assert not diag.score_place(
        [("x", "show_view", {"action": "fly_to", "location": "three-rivers"})], "trois"
    ), "une traduction n'est pas le lieu demandé"


def test_correct_calls_are_scored_as_successes() -> None:
    diag = _diag()
    assert diag.score_place(
        [("x", "show_view", {"action": "fly_to", "location": "Trois-Rivières"})], "trois"
    )
    assert diag.score_place(
        [("x", "map_control", {"action": "fly_to", "location": "Montréal"})], "montreal"
    ), "accents normalisés, les deux outils de carte acceptés"
    assert diag.score_view(
        [("x", "show_view", {"action": "show", "view_id": "cockpit"})],
        "system-monitor",
        _resolve_view_id,
    ), "l'alias cockpit doit compter"


def test_diag_menu_sees_the_real_tools() -> None:
    names = {s["name"] for s in _diag().load_schemas(_ROOT / "src/jarvis/capabilities/tools")}
    assert {"show_view", "map_control", "get_weather"} <= names
    assert "" not in names, "la classe de base Tool (name vide) n'est pas un outil"
    assert len(names) >= 20
