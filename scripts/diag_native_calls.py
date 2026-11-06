# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Mesure : Qwen appelle-t-il ses outils NATIVEMENT avec le vrai prompt de Jarvis ?

Pourquoi ce script : tout le mécanisme d'appel du chat (l'analyseur de texte, la
relance forcée, le détecteur d'affirmations) repose sur une conclusion ancienne,
« qwen3 n'émet aucun appel natif ». Elle a été mesurée avec un seul outil et le
seul prompt statique. Personne ne l'a refaite dans les conditions réelles.

Ce script :
- construit Jarvis exactement comme en production (bootstrap.build(), sans réseau
  ni tâche de fond) et prend SON prompt système et SES 29 outils ;
- envoie des phrases réelles à Ollama, avec la même requête que le chat
  (num_ctx, num_predict, think=false, température 0.7) ;
- n'exécute AUCUN outil : il lit seulement ce que le modèle demande ;
- compare deux variantes du prompt :
    texte  = le prompt actuel (consigne « ÉCRIS l'appel ») ;
    natif  = le même, sans cette consigne ;
- répète chaque phrase N fois, parce qu'un modèle à 0.7 ne répond pas deux fois
  pareil : on mesure un TAUX, pas un oui/non.

Usage (Jarvis peut rester allumé, mais la mesure ira plus vite s'il est éteint) :
    .\\.venv\\Scripts\\python.exe scripts\\diag_native_calls.py
    .\\.venv\\Scripts\\python.exe scripts\\diag_native_calls.py --runs 5 --variant natif

Résultats : tableau à l'écran + scripts/diag_results/native_calls_<date>.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@dataclass(frozen=True)
class Case:
    phrase: str
    expected: frozenset[str]  # vide = aucun outil attendu
    args: dict = field(default_factory=dict)  # arguments qui doivent valoir ça


CASES = [
    Case("joue Red House", frozenset({"spotify_control"})),
    Case("mets en pause", frozenset({"spotify_control"})),
    Case("montre-moi Paris", frozenset({"map_control", "show_view"})),
    Case("quelle heure il est", frozenset()),
    Case("quel temps fait-il à Montréal ?", frozenset({"get_weather"})),
    Case("qu'est-ce que j'ai à l'agenda cette semaine ?", frozenset({"list_calendar_events"})),
    Case(
        "ajoute un rendez-vous chez le dentiste jeudi à 15h",
        frozenset({"create_calendar_event"}),
    ),
    Case("montre mes courriels non lus", frozenset({"list_emails"})),
    Case("lis le premier courriel non lu", frozenset({"read_email"})),
    Case("lis le dernier courriel de Jean Coutu", frozenset({"read_email"})),
    Case(
        "archive le courriel de Jean Coutu",
        frozenset({"manage_emails"}),
        {"action": "archive"},
    ),
    Case(
        "mets à la corbeille le courriel Test Jarvis",
        frozenset({"manage_emails"}),
        {"action": "trash"},
    ),
    Case(
        "envoie un courriel à lgliamgauthier@gmail.com avec le sujet Test et le texte Bonjour",
        frozenset({"send_email"}),
    ),
    Case("c'est quoi la capitale de l'Australie ?", frozenset()),
    Case("merci !", frozenset()),
]

_TEXT_BLOCK = re.compile(
    r"## Comment déclencher un outil — ÉCRIS l'appel.*?(?=\n\n#{1,2} (?!#)|\Z)", re.S
)


def _verdict(case: Case, native: list[dict], text: list[tuple[str, dict]]) -> str:
    def ok(name: str, args: dict) -> bool:
        if name not in case.expected:
            return False
        return all(str(args.get(k, "")).lower() == v for k, v in case.args.items())

    if not case.expected:
        return "ok" if not native and not text else "appel_en_trop"
    if any(ok(c["name"], c["args"]) for c in native):
        return "natif_ok"
    if any(ok(n, a) for n, a in text):
        return "texte_ok"
    if native or text:
        return "mauvais_appel"
    return "aucun_appel"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--variant", choices=["texte", "natif", "les_deux"], default="les_deux")
    parser.add_argument(
        "--filtre", default="", help="ne rejouer que les phrases contenant ce texte"
    )
    opts = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    from loguru import logger

    logger.remove()  # la construction de Jarvis est bavarde ; seul le tableau compte

    import httpx

    from jarvis.bootstrap import build

    jarvis = build()
    agent, llm, settings = jarvis.agent, jarvis.llm, jarvis.settings
    tools = jarvis.tool_registry.schemas()
    names = {t["name"] for t in tools}
    missing = sorted({n for c in CASES for n in c.expected} - names)
    if missing:
        print(f"Outils attendus absents du registre : {missing}")
        return 2

    prompt_texte = agent._build_system()
    prompt_natif = _TEXT_BLOCK.sub("", prompt_texte)
    if "ÉCRIS l'appel" in prompt_natif:
        print("La consigne « ÉCRIS l'appel » n'a pas pu être retirée : variante natif invalide.")
        return 2
    variants = {"texte": prompt_texte, "natif": prompt_natif}
    if opts.variant != "les_deux":
        variants = {opts.variant: variants[opts.variant]}

    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    print(
        f"Modèle {settings.ollama_model} · {len(tools)} outils · "
        f"num_ctx {settings.ollama_num_ctx}"
    )
    print(f"Prompt : {len(prompt_texte)} car. (texte) / {len(prompt_natif)} car. (natif)\n")

    report: dict = {"date": datetime.now().isoformat(), "model": settings.ollama_model,
                    "runs": opts.runs, "variants": {}}
    with httpx.Client(timeout=180.0) as client:
        for label, system in variants.items():
            rows = []
            for case in CASES:
                if opts.filtre and opts.filtre.lower() not in case.phrase.lower():
                    continue
                for run in range(opts.runs):
                    messages = [{"role": "user", "content": f"{case.phrase}\n[voix]"}]
                    payload = llm._payload(messages, system, stream=False, tools=tools)
                    start = time.perf_counter()
                    resp = client.post(url, json=payload)
                    if resp.status_code == 400 and "think" in resp.text.lower():
                        payload.pop("think", None)
                        resp = client.post(url, json=payload)
                    resp.raise_for_status()
                    seconds = time.perf_counter() - start
                    data = resp.json()
                    msg = data.get("message", {})
                    native = []
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {"_brut": args}
                        native.append({"name": fn.get("name", ""), "args": args})
                    content = msg.get("content", "") or ""
                    text = [(n, a) for _, n, a in agent.extract_text_tool_calls(content)]
                    verdict = _verdict(case, native, text)
                    rows.append({
                        "phrase": case.phrase, "run": run + 1, "verdict": verdict,
                        "secondes": round(seconds, 1),
                        "jetons_prompt": data.get("prompt_eval_count"),
                        "natif": native, "texte": [{"name": n, "args": a} for n, a in text],
                        "contenu": content[:300],
                    })
                    print(
                        f"[{label}] {case.phrase[:48]:<48} #{run + 1}  "
                        f"{verdict:<14} {seconds:5.1f} s"
                    )
            report["variants"][label] = rows

    print("\n=== Synthèse ===")
    for label, rows in report["variants"].items():
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        total = len(rows)
        good = counts.get("natif_ok", 0) + counts.get("texte_ok", 0) + counts.get("ok", 0)
        avg = sum(r["secondes"] for r in rows) / total
        print(f"{label:<6} réussis {good}/{total} · " + " · ".join(
            f"{k} {v}" for k, v in sorted(counts.items())) + f" · moyenne {avg:.1f} s")

    out_dir = _ROOT / "scripts" / "diag_results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"native_calls_{datetime.now():%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDétail : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
