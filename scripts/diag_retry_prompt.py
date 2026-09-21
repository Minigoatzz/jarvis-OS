# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Mesure le prompt de relance (`force_tool_call`) sur le VRAI modèle.

Pourquoi ce script : ce prompt a été retouché trois fois à l'intuition, et
chaque retouche a introduit un défaut (menu de 13 Ko, Reykjavik recopié, enums
masquées). Plus de retouche sans mesure.

Conditions de production reproduites :
  - le prompt vient de `build_retry_prompt`, la fonction qu'utilise Jarvis ;
  - le menu est bâti sur les vrais outils de src/jarvis/capabilities/tools ;
  - même modèle, même num_ctx (lus dans .env), temperature 0.7, think=False ;
  - la relance n'envoie QUE le message utilisateur, sans historique : ici aussi ;
  - l'appel est lu par l'analyseur de Jarvis, pas par une regex à part.

Le modèle échantillonne à 0.7 : un seul tirage ne prouve rien. Chaque phrase
est donc envoyée N fois et on compte un TAUX de réussite.

Usage, depuis C:\\jarvis-OS — avec le Python QUE JARVIS UTILISE (voir
Get-JarvisPython dans jarvis.ps1). `bundle\\python\\python.exe` est
l'interpréteur nu, sans aucun paquet : ModuleNotFoundError: loguru.
    .\\bundle\\.venv\\Scripts\\python.exe scripts\\diag_retry_prompt.py
    .\\bundle\\.venv\\Scripts\\python.exe scripts\\diag_retry_prompt.py --n 6
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (phrase, jeton qui doit se retrouver dans location) — lieux réels, dont les
# cinq qui ont échoué le 21/09 et l'adresse qui a réussi.
PLACES = [
    ("montre moi paris", "paris"),
    ("montre moi trois-rivières", "trois"),
    ("montre moi sainte-flore", "flore"),
    ("montre moi le 655 rue des fauvettes à longueuil", "fauvettes"),
    ("montre moi whistler, BC", "whistler"),
    ("montre moi la tour eiffel", "eiffel"),
    ("montre moi moscou", "moscou"),
]
# (phrase, identifiant de vue attendu) — ne doivent PAS partir en fly_to.
VIEWS = [
    ("montre moi la météo", "weather"),
    ("montre moi le cockpit", "system-monitor"),
]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def load_schemas(tools_dir: Path) -> list[dict]:
    """Schémas des outils, lus dans le source (aucune instanciation)."""
    schemas: list[dict] = []
    for path in sorted(tools_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            values: dict = {}
            for stmt in node.body:
                target = value = None
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target, value = stmt.target.id, stmt.value
                elif (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                ):
                    target, value = stmt.targets[0].id, stmt.value
                if target in ("name", "description", "input_schema") and value is not None:
                    try:
                        values[target] = ast.literal_eval(value)
                    except ValueError:
                        continue
            name = values.get("name")
            # La classe de base `Tool` déclare name = "" : ce n'est pas un outil.
            if isinstance(name, str) and name.strip() and isinstance(values.get("description"), str):
                schemas.append(
                    {
                        "name": values["name"],
                        "description": values["description"],
                        "input_schema": values.get("input_schema") or {},
                    }
                )
    return schemas


def score_place(calls: list, token: str) -> bool:
    if not calls:
        return False
    _cid, name, args = calls[0]
    return (
        name in ("show_view", "map_control")
        and str(args.get("action", "")) == "fly_to"
        and _norm(token) in _norm(args.get("location", ""))
    )


def score_view(calls: list, expected: str, resolve) -> bool:
    if not calls:
        return False
    _cid, name, args = calls[0]
    view_id = args.get("view_id")
    return (
        name == "show_view"
        and str(args.get("action", "")) == "show"
        and isinstance(view_id, str)
        and resolve(view_id) == expected
    )


def describe(calls: list, raw: str) -> str:
    if not calls:
        return f"aucun appel — brut : {raw.strip()[:70]!r}"
    _cid, name, args = calls[0]
    return f"{name}({json.dumps(args, ensure_ascii=False)})"[:110]


def _ollama(base_url: str, payload: dict) -> str:
    def post(body: dict) -> dict:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = post(payload)
    except urllib.error.HTTPError as exc:
        if exc.code != 400 or "think" not in payload:
            raise
        # Même repli que la prod : un modèle qui refuse le champ « think ».
        data = post({k: v for k, v in payload.items() if k != "think"})
    return data["message"]["content"]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # console Windows
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=4, help="tirages par phrase (défaut 4)")
    parser.add_argument(
        "--variants", default="sans,lieux", help="sans (règle des lieux coupée), lieux (production), ou les deux"
    )
    args = parser.parse_args()

    os.chdir(ROOT)  # settings lit .env relativement au dossier courant
    sys.path.insert(0, str(ROOT / "src"))
    from loguru import logger

    logger.remove()
    from jarvis.capabilities.tools.show_view import _resolve_view_id
    from jarvis.engine.agent import Agent, build_retry_prompt
    from jarvis.providers.llm.local import _strip_think
    from jarvis.kernel.settings import settings

    schemas = load_schemas(ROOT / "src" / "jarvis" / "capabilities" / "tools")

    class _Registry:
        def has_tools(self) -> bool:
            return True

        def schemas(self) -> list[dict]:
            return schemas

    parser_agent = Agent.__new__(Agent)
    parser_agent._tool_registry = _Registry()

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    cases = [("lieu", p, t) for p, t in PLACES] + [("vue", p, v) for p, v in VIEWS]
    print(
        f"Modèle {settings.ollama_model} @ {settings.ollama_base_url} — num_ctx "
        f"{settings.ollama_num_ctx} — {len(schemas)} outils — {args.n} tirages/phrase"
    )
    print(f"{len(cases) * args.n * len(variants)} appels au total, compte ~5 s chacun.\n")

    totals: dict[str, list[int]] = {}
    for variant in variants:
        system = build_retry_prompt(schemas, place_rules=(variant == "lieux"))
        print(f"══ variante « {variant} » ({len(system)} caractères) ══")
        ok_all = 0
        for kind, phrase, expected in cases:
            ok, misses = 0, []
            for _ in range(args.n):
                payload = {
                    "model": settings.ollama_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": phrase},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.7, "num_ctx": settings.ollama_num_ctx},
                }
                raw = _strip_think(_ollama(settings.ollama_base_url, payload))
                calls = parser_agent.extract_text_tool_calls(raw)
                good = (
                    score_place(calls, expected)
                    if kind == "lieu"
                    else score_view(calls, expected, _resolve_view_id)
                )
                ok += good
                if not good:
                    misses.append(describe(calls, raw))
            ok_all += ok
            print(f"  {ok}/{args.n}  {phrase}")
            for miss in sorted(set(misses))[:2]:
                print(f"         ✗ {miss}")
        totals[variant] = [ok_all, len(cases) * args.n]
        print()

    print("══ RÉSUMÉ ══")
    for variant, (ok, total) in totals.items():
        print(f"  {variant:7} {ok}/{total}  ({100 * ok // total} %)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
