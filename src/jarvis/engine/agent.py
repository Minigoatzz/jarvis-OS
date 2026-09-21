# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from loguru import logger

from jarvis.engine.session import Session
from jarvis.kernel.contracts import (
    LLMProvider,
    MemoryIndex,
    SkillRegistry,
    ToolRegistry,
    TopicStore,
)
from jarvis.kernel.paths import PROMPTS_DIR
from jarvis.kernel.schemas import ToolCapture
from jarvis.kernel.settings import Settings

_STATIC_PROMPT_PATH = PROMPTS_DIR / "system_static.md"
_MAX_TOOL_RESULT_CHARS = 12_000

# ToolRegistry.call() préfixe le contenu d'un outil en échec par son code JRV
# (_prefix_error_content). C'est le SEUL marqueur d'échec qui survit jusqu'ici :
# call_str() rend une chaîne, le drapeau is_error de ToolResult ne traverse pas.
_TOOL_ERROR_RE = re.compile(r"^\[JRV-[A-Z]+-\d+\]")


# Formules par lesquelles le modèle affirme qu'une action a eu lieu. Toutes
# relevées dans des échanges réels où AUCUN outil n'avait tourné : « C'est
# lancé, le cockpit est affiché. », « C'est fait. », « Voilà la météo. »
_COMPLETION_CLAIM_RE = re.compile(
    r"(c'est\s+(lanc|fait|pars|bon|pause|paus)"
    r"|voil[àa]\s"
    r"|est\s+(affich|lanc|activ)"
    r"|j'ai\s+(lanc|affich|ouvert|mis|activ)"
    r"|je\s+lance\b"
    r"|c'est\s+parti)",
    re.IGNORECASE,
)


# Un jeton entre crochets, seul sur la ligne : « [Cockpit] », « [Globe] ».
# Pas d'espace à l'intérieur — « [MINDMAP] ceci est du contenu » reste du
# contenu légitime et ne doit PAS être capturé ici.
_BRACKET_ONLY_RE = re.compile(r"^\[[^\]\s]{1,24}\]$")


def is_degenerate_reply(text: str) -> bool:
    """True si la réponse n'est pas une réponse — vide, ou un tag nu.

    Observé le 18/09 : « montre moi la tour eiffel » a renvoyé exactement
    « [Cockpit] », rien d'autre. Le modèle a appris du prompt que les réponses
    commencent par « [TAG] » ; faute de savoir quoi faire, il émet un jeton
    entre crochets et s'arrête. `_ANY_TAG_RE` (router.py) ne filtre que les
    tags COURTS en MAJUSCULES — `[C]`, `[A]` — pour épargner du contenu réel
    comme `[MINDMAP]`. « [Cockpit] » fait sept lettres : il traverse.

    Aucun garde-fou existant ne l'attrapait : la route retombe sur [I] faute
    de tag valide, et `claims_completion("[Cockpit]")` est faux. La sortie
    partait donc telle quelle à l'utilisateur.

    C'est la même panne que « [outil appelé] » et que les accusés de réception
    « [BG:PROJECT] » : le modèle recopie une notation à crochets vue ailleurs.
    """
    stripped = text.strip()
    if not stripped:
        return True
    return bool(_BRACKET_ONLY_RE.match(stripped))


_JRV_PREFIX_RE = re.compile(r"^\[JRV-[A-Z]+-\d+\]\s*")


def all_tools_failed_message(names: list[str], results: list[str]) -> str | None:
    """Réponse honnête quand CHAQUE outil du tour a échoué, sinon None.

    Observé le 21/09 à 07:19 : « Montre-moi Montréal » → show_view a renvoyé
    une erreur, et la synthèse a répondu « C'est lancé, on est sur Montréal. »
    La consigne « UN OUTIL VIENT D'ÉCHOUER » ajoutée au prompt de synthèse
    n'a pas suffi : on ne demande plus au modèle de résumer un échec total,
    on le dit nous-mêmes. Un succès partiel reste confié à la synthèse.
    """
    if not results or not all(_is_tool_error(r) for r in results):
        return None
    reasons = []
    for name, result in zip(names, results):
        reason = _JRV_PREFIX_RE.sub("", result.strip()).strip() or "erreur sans détail"
        reasons.append(f"{name} : {reason}")
    return (
        "Ça n'a pas marché, rien n'a changé — "
        + " ; ".join(reasons)
        + ". Reformule et je réessaie."
    )


def claims_completion(text: str) -> bool:
    """True si la réponse affirme qu'une action a été effectuée.

    Sert d'unique déclencheur au garde-fou du gateway : une affirmation
    d'action sans outil exécuté est un mensonge, et c'est la dernière forme
    qu'il restait. On ne se fie PAS au tag de routing pour ça — « montre moi le
    cockpit » a été taggé [I], donc hors du repli réservé à [CF], et la phrase
    « C'est lancé, le cockpit est affiché. » est partie telle quelle alors que
    le journal ne montre aucun `Tool executed` sur ce tour.
    """
    return bool(_COMPLETION_CLAIM_RE.search(text))


def _is_tool_error(result: str) -> bool:
    """True si ce résultat d'outil est un échec.

    Sans cette lecture, la synthèse recevait un échec sous la forme exacte d'un
    succès : « [JRV-TOL-004] Playlist trouvée mais impossible de lancer (404) »
    n'est que du texte pour le modèle, qui enchaînait « C'est lancé. » pendant
    que la musique ne bougeait pas.
    """
    return bool(_TOOL_ERROR_RE.match(result.strip()))


def _scan_balanced_parens(text: str, open_idx: int) -> str | None:
    """Retourne le contenu entre la parenthèse ouvrante et sa fermante.

    Suit l'état des guillemets : `execute_cli(command="open -a 'Safari'")` contient
    des apostrophes imbriquées, et une parenthèse peut apparaître dans une chaîne.
    Retourne None si la parenthèse n'est jamais refermée (sortie tronquée).
    """
    depth = 0
    quote: str | None = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                # Saute VRAIMENT le caractère échappé. Sans le +2, le guillemet
                # de `command="osascript -e 'tell application \"Spotify\" ...'"`
                # refermait la chaîne trop tôt et les arguments partaient en
                # morceaux — sortie réellement produite par qwen3:14b.
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
        i += 1
    return None


def _scan_balanced_braces(text: str, open_idx: int) -> str | None:
    """Retourne l'objet JSON complet, accolades comprises, depuis `open_idx`.

    Même logique que _scan_balanced_parens : une accolade dans une chaîne ne
    compte pas. Retourne None si l'objet n'est jamais refermé.
    """
    depth = 0
    quote: str | None = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch == '"':
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
        i += 1
    return None


_ARG_RE = re.compile(
    r"""(\w+)\s*=\s*(   "(?:[^"\\]|\\.)*"      # chaîne entre guillemets doubles
                      | '(?:[^'\\]|\\.)*'      # chaîne entre guillemets simples
                      | [^,]+                  # nombre, booléen, mot nu
                    )""",
    re.VERBOSE,
)


def _coerce_arg(raw: str) -> object:
    """Convertit une valeur d'argument textuelle en type Python."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].replace('\\"', '"').replace("\\'", "'")
    low = value.lower()
    if low in ("true", "vrai"):
        return True
    if low in ("false", "faux"):
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(value)
    except ValueError:
        # jrv: sondage de type, pas une panne — une valeur non entière est le cas
        # nominal ici. Émettre un code d'erreur ferait du bruit à chaque argument
        # textuel. Volontairement non mappé (scripts/error_audit/scan.py).
        pass
    try:
        return float(value)
    except ValueError:
        # jrv: idem, sondage de type (voir ci-dessus).
        pass
    return value


def _parse_call_args(args_raw: str) -> dict:
    """Parse les arguments d'un appel ecrit en texte.

    Deux formes, toutes deux produites par qwen3:14b :
      - `action="pause", level=50`        -> clef=valeur
      - `{"action": "fly_to", "zoom": 12}` -> objet JSON entre les parentheses

    La seconde renvoyait un dict VIDE : `map_control({"action": "fly_to"})` etait
    bien reconnu comme un appel a map_control, puis execute SANS action, donc en
    echec. Le globe ne bougeait pas et la panne ressemblait a un outil absent.
    """
    blob = args_raw.strip()
    if blob.startswith("{") and blob.endswith("}"):
        try:
            parsed = json.loads(blob)
        except ValueError:
            # jrv: pas du JSON valide malgre les accolades — on retombe sur le
            # parsing clef=valeur ci-dessous. Cas nominal, volontairement non
            # mappe (scripts/error_audit/scan.py).
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {m.group(1): _coerce_arg(m.group(2)) for m in _ARG_RE.finditer(args_raw)}


_MENU_DESC_LIMIT = 140
_MENU_ENUM_LIMIT = 6
_SENTENCE_RE = re.compile(r"(.+?[.!?])(\s|$)", re.DOTALL)


def _first_sentence(text: str, limit: int = _MENU_DESC_LIMIT) -> str:
    """Première phrase d'une description d'outil, tronquée."""
    first = text.strip().split("\n")[0].strip()
    match = _SENTENCE_RE.match(first)
    sentence = match.group(1) if match else first
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rstrip() + "…"


def _signature(schema: object) -> str:
    """`(action=a|b, [option])` — requis nus, optionnels entre crochets.

    Les valeurs d'enum sont ce dont le modèle a réellement besoin pour écrire
    un appel valide : `action=fly_to|zoom_in|…` vaut mieux qu'un paragraphe.
    Elles n'étaient visibles nulle part dans l'ancien menu.
    """
    if not isinstance(schema, dict):
        return "()"
    props = schema.get("properties")
    if not isinstance(props, dict):
        return "()"
    required = set(schema.get("required") or [])
    parts: list[str] = []
    for key, spec in props.items():
        rendered = str(key)
        if isinstance(spec, dict):
            values = spec.get("enum")
            # Un argument REQUIS garde toujours sa liste complète : c'est la
            # valeur que le modèle DOIT choisir. Le plafond ne vaut que pour
            # les optionnels. Régression du 21/09 : show_view a 8 actions, le
            # plafond de 6 les masquait toutes, et la relance montrait
            # `show_view(action, ...)` sans aucune valeur permise. Le modèle
            # devinait (« display », « show » sans view_id) et l'outil échouait.
            if isinstance(values, list) and values and (
                key in required or len(values) <= _MENU_ENUM_LIMIT
            ):
                rendered = f"{key}={'|'.join(str(v) for v in values)}"
        parts.append(rendered if key in required else f"[{rendered}]")
    return "(" + ", ".join(parts) + ")"


def _compact_tool_menu(schemas: list[dict]) -> str:
    """Menu d'outils minimal — une ligne par outil, signature comprise.

    Mesuré le 18/09 sur les 27 outils enregistrés : le menu bâti à partir des
    descriptions complètes pèse 13 076 caractères (~3 270 tokens), dont 3 430
    pour le seul `fusion_360` — plus que les dix outils suivants réunis.
    `force_tool_call` se décrivait comme « un prompt réduit au menu d'outils »
    alors qu'il envoyait ce mur ; l'ablation qui justifiait la relance, elle,
    utilisait un prompt réellement court. Cette version fait ~3 300 caractères
    (~820 tokens) ET expose les enums, invisibles auparavant.
    """
    lines: list[str] = []
    for schema in schemas:
        name = str(schema.get("name", "")).strip()
        if not name:
            continue
        desc = _first_sentence(str(schema.get("description", "")))
        lines.append(f"- `{name}{_signature(schema.get('input_schema'))}` : {desc}")
    return "\n".join(lines)


def _clip_tool_result(text: str) -> str:
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    return (
        text[:_MAX_TOOL_RESULT_CHARS]
        + f"\n...[truncated, {len(text)} characters total]"
    )


class Agent:
    """Construit le prompt (static + dynamic), appelle le LLM, retourne le stream.

    Phase C : `settings` injecté au constructeur (auparavant
    `from config.settings import settings as _s` en local dans
    `_build_system()`). Les autres dépendances (llm, memory_index,
    topic_store, tool_registry, skill_registry, user_prefs_path,
    user_model_path) étaient déjà injectées en Phase pré-C.

    Note CYCLE 1 (CDC §C.1.3) : `from jarvis.providers.llm.api import
    ToolCapture` au top-level franchit la couche engine → providers.
    Cette dépendance sera résolue dans un commit dédié post-gateway
    en faisant remonter `ToolCapture` (et `UsageEntry`, `calculate_cost`)
    dans `kernel/`. Hors-périmètre du commit présent.
    """

    def __init__(
        self,
        settings: Settings,
        llm: LLMProvider,
        memory_index: MemoryIndex | None = None,
        topic_store: TopicStore | None = None,
        tool_registry: ToolRegistry | None = None,
        user_prefs_path: Path | None = None,
        skill_registry: SkillRegistry | None = None,
        user_model_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._memory_index = memory_index
        self._topic_store = topic_store
        self._tool_registry = tool_registry
        self._user_prefs_path = user_prefs_path
        self._skill_registry = skill_registry
        self._user_model_path = user_model_path

    def _build_system(
        self,
        notifications: list[str] | None = None,
        recall_summary: str | None = None,
    ) -> str:
        """Assemble le prompt système : partie statique + contexte dynamique."""
        _s = self._settings

        static_system = _STATIC_PROMPT_PATH.read_text(encoding="utf-8")
        # Le prompt statique est rédigé avec "Barth" comme nom par défaut ; on le
        # remplace par le prénom configuré (USER_FIRSTNAME) pour que l'assistant appelle
        # réellement l'utilisateur par son nom. Repli sur "Barth" si non configuré.
        firstname = _s.display_name
        if firstname != "Barth":
            static_system = static_system.replace("Barth", firstname)
        # Remplace "Jarvis" par le nom de l'assistant configuré (ASSISTANT_NAME)
        assistant_name = _s.display_assistant_name
        if assistant_name != "Jarvis":
            static_system = static_system.replace("Jarvis", assistant_name)
        if _s.quebec_mode:
            static_system += (
                "\n\n## Mode Québécois (ACTIF)\n"
                "Tu parles avec un accent et du dialecte québécois authentique. "
                "Utilise : 'ostie', 'câlice', 'tabarnak' (avec parcimonie),"
                " 'c'est le boutte', 'en masse', 'pantoute', 'tantôt', 'maudit', 'icitte',"
                " 'chu' (je suis), 'ben' (bien), 'toé', 'moé', 'faque', 't'sé',"
                " 'un char' (voiture), 'magasiner' (shopping). "
                f"Garde la personnalité {assistant_name} (direct, efficace, ironie)"
                " avec la couleur québécoise."
            )
        dynamic_parts: list[str] = ["=== CONTEXTE DYNAMIQUE ==="]

        # Identité LLM — indispensable pour les modèles locaux qui ne savent pas ce qu'ils sont
        if _s.llm_provider == "local":
            llm_id = f"Ollama / {_s.ollama_model}"
        else:
            _model_map = {
                "anthropic": _s.anthropic_model,
                "mistral": _s.mistral_model,
                "openai": _s.openai_model,
            }
            llm_id = _model_map.get(_s.api_backend, _s.anthropic_model)
        dynamic_parts.append(f"## Moteur LLM actif\n\nTu tournes sur **{llm_id}**.")

        # Date/heure toujours injectée — utile pour le calendrier et les calculs temporels
        now = datetime.now()
        dynamic_parts.append(f"## Date et heure\n\n{now.strftime('%Y-%m-%d %H:%M')}")

        if recall_summary:
            dynamic_parts.append(f"## Rappel de sessions précédentes\n\n{recall_summary}")

        if self._user_model_path is not None and self._user_model_path.exists():
            model_text = self._user_model_path.read_text(encoding="utf-8").strip()
            if model_text:
                dynamic_parts.append(f"## Modèle utilisateur\n\n{model_text}")

        if self._user_prefs_path is not None and self._user_prefs_path.exists():
            prefs = self._user_prefs_path.read_text(encoding="utf-8").strip()
            if prefs:
                dynamic_parts.append(f"## Préférences {firstname}\n\n{prefs}")

        if self._memory_index is not None:
            index_content = self._memory_index.read()
            dynamic_parts.append(f"## Mémoire index\n\n{index_content}")

        if self._topic_store is not None:
            topic_names = self._topic_store.list_all()
            if topic_names:
                names_list = "\n".join(f"- `{name}`" for name in topic_names)
                dynamic_parts.append(
                    "## Fichiers thématiques disponibles\n\n"
                    "Ces fichiers ne sont PAS préchargés. Pour les consulter, utilise "
                    "`memory_search` (recherche sémantique) puis `memory_load_topic(filename=...)` "
                    "pour lire un fichier complet si nécessaire (routing [CF]).\n\n"
                    f"{names_list}"
                )

        if self._tool_registry is not None and self._tool_registry.has_tools():
            tool_lines = "\n".join(
                f"- `{s['name']}` : {s['description']}" for s in self._tool_registry.schemas()
            )
            dynamic_parts.append(
                f"## Outils disponibles (router [CF] pour les utiliser)\n\n{tool_lines}"
            )
            # Le transport d'appel RÉEL en mode local, c'est le texte.
            #
            # Ce bloc disait l'inverse : « ÉMETS UN APPEL NATIF / n'écris JAMAIS
            # l'appel en texte ». Deux raisons de l'annuler :
            #   - la campagne d'ablation (scripts/diag_native_tools.ps1) a mesuré
            #     exactement cette contre-instruction : elle ne produit AUCUN
            #     tool_call natif, à aucune des exécutions ;
            #   - son argument (« le gateway n'exécute que le natif ») n'est plus
            #     vrai : l'analyseur de texte du gateway exécute la ligne écrite.
            # Résultat observé : le modèle obéissait à l'interdiction, n'écrivait
            # plus rien, n'émettait toujours rien nativement — et répondait
            # « C'est fait. » pendant que la musique continuait.
            if _s.llm_provider == "local":
                dynamic_parts.append(
                    "## Comment déclencher un outil — ÉCRIS l'appel\n\n"
                    "Pour utiliser un outil, écris son appel sur sa propre ligne, "
                    "exactement sous cette forme :\n\n"
                    'spotify_control(action="pause")\n\n'
                    "Écrire cette ligne EXÉCUTE l'outil : Jarvis la lit, l'exécute, "
                    "puis te redonne le résultat pour que tu formules ta réponse. "
                    "Sans cette ligne, RIEN ne s'exécute.\n\n"
                    "Donc, règle absolue : n'écris jamais qu'une action est faite "
                    "(« c'est lancé », « c'est fait », « voilà ») si tu n'as pas "
                    "écrit l'appel correspondant dans la même réponse.\n\n"
                    "### Routage des actions\n\n"
                    "Une action immédiate sur une app, un appareil ou l'écran — "
                    "musique, carte, globe, vue, météo, ouvrir quelque chose — "
                    "c'est `[CF]` SUIVI DE L'APPEL. Jamais `[BG:PROJECT]`.\n\n"
                    "`[BG:PROJECT]` sert uniquement à PRODUIRE DES FICHIERS que "
                    f"{firstname} relira : documents, scripts, emails rédigés. "
                    "« Montre-moi Paris », « joue Red House », « mets en pause », "
                    "« montre la météo » ne produisent aucun fichier — ce sont "
                    "des `[CF]`, et répondre « C'est lancé, suis l'avancement dans "
                    "le dashboard » à ces demandes est une erreur."
                )

        if self._skill_registry is not None:
            skills_prompt = self._skill_registry.get_combined_system_prompt()
            if skills_prompt:
                dynamic_parts.append("# SKILLS ACTIFS\n\n" + skills_prompt)

        if notifications:
            notif_content = "\n".join(f"- {n}" for n in notifications)
            dynamic_parts.append(
                f"## Notifications en attente — À GLISSER EN FIN DE RÉPONSE\n\n{notif_content}"
            )

        return static_system + "\n\n" + "\n\n".join(dynamic_parts)

    def has_tools(self) -> bool:
        return (
            self._tool_registry is not None
            and self._tool_registry.has_tools()
            and self._llm.supports_tools
        )

    def _find_text_tool_calls(self, text: str) -> list[tuple[int, int, str, dict]]:
        """Localise les appels d'outils ÉCRITS EN TEXTE — (début, fin, nom, args).

        Le prompt statique définit lui-même ce format, illustré par des exemples
        comme `execute_cli(command="open -a 'Safari'")`. Claude les lit comme une
        intention et appelle nativement ; un modèle local les prend au pied de la
        lettre et écrit littéralement l'appel. La ligne s'affichait alors dans la
        conversation et rien ne s'exécutait.

        DEUX notations sont observées, sur le même prompt et le même modèle :
          - appel de fonction : spotify_control(action="pause")
          - objet JSON        : {"name": "spotify_control", "arguments": {...}}
        La seconde traversait l'analyseur sans laisser de trace — la demande
        disparaissait en silence, exactement comme avant l'ajout du repli texte.

        Sécurité : seuls les noms d'outils RÉELLEMENT enregistrés sont reconnus,
        et les arguments passent par le ToolRegistry qui les valide comme
        n'importe quel appel — rien n'est évalué comme du code.
        """
        if self._tool_registry is None or not text.strip():
            return []

        names = {str(s.get("name", "")) for s in self._tool_registry.schemas()}
        names.discard("")
        if not names:
            return []

        found: list[tuple[int, int, str, dict]] = []

        # ── Notation appel de fonction ──────────────────────────────────────
        for name in names:
            for match in re.finditer(rf"\b{re.escape(name)}\s*\(", text):
                open_idx = match.end() - 1
                args_raw = _scan_balanced_parens(text, open_idx)
                if args_raw is None:
                    continue
                stop = open_idx + len(args_raw) + 2  # '(' + contenu + ')'
                found.append((match.start(), stop, name, _parse_call_args(args_raw)))

        # ── Notation JSON ───────────────────────────────────────────────────
        for brace in re.finditer(r"\{", text):
            blob = _scan_balanced_braces(text, brace.start())
            if blob is None:
                continue
            try:
                payload = json.loads(blob)
            except ValueError:
                # jrv: la plupart des accolades d'un texte ne sont pas du JSON.
                # C'est le cas nominal, pas une panne — émettre un code d'erreur
                # ferait du bruit à chaque accolade. Volontairement non mappé
                # (scripts/error_audit/scan.py).
                continue
            if not isinstance(payload, dict):
                continue
            name = payload.get("name")
            if not isinstance(name, str) or name not in names:
                continue
            args = payload.get("arguments")
            if args is None:
                args = payload.get("parameters")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    # jrv: arguments illisibles — on garde l'appel, le
                    # ToolRegistry refusera proprement s'il en manque.
                    args = {}
            found.append(
                (
                    brace.start(),
                    brace.start() + len(blob),
                    name,
                    args if isinstance(args, dict) else {},
                )
            )

        found.sort(key=lambda item: item[0])

        # Un appel imbriqué dans un autre (objet JSON dans un objet JSON) ne doit
        # pas être exécuté deux fois.
        kept: list[tuple[int, int, str, dict]] = []
        for span in found:
            if kept and span[0] < kept[-1][1]:
                continue
            kept.append(span)
        return kept

    def extract_text_tool_calls(self, text: str) -> list[tuple[str, str, dict]]:
        """Appels écrits en texte, au format de ToolCapture.calls — (id, nom, args)."""
        return [
            (f"text_{name}_{i}", name, args)
            for i, (_start, _stop, name, args) in enumerate(self._find_text_tool_calls(text))
        ]

    def strip_text_tool_calls(self, text: str) -> str:
        """Retire du texte les appels écrits en toutes lettres.

        Deux usages, tous deux nécessaires :
          - ce que voit l'utilisateur : il a demandé « dis-moi que tu mets la
            musique en pause », pas de lire la commande ;
          - ce qui est rendu au modèle : lui resservir sa propre notation comme
            étant sa parole l'encourage à recommencer au tour suivant.
        L'EXÉCUTION, elle, se fait toujours sur le texte intact.
        """
        spans = self._find_text_tool_calls(text)
        if not spans:
            return text
        out: list[str] = []
        pos = 0
        for start, stop, _name, _args in spans:
            out.append(text[pos:start])
            pos = stop
        out.append(text[pos:])
        return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()

    def mentions_tool_call_in_text(self, text: str) -> bool:
        """True si le texte contient au moins un appel d'outil écrit en toutes lettres."""
        return bool(self._find_text_tool_calls(text))

    async def respond(
        self,
        session: Session,
        user_message: str,
        stream: bool = True,
        notifications: list[str] | None = None,
    ) -> str | AsyncIterator[str]:
        """Routing-only pass : ajoute le message, appelle le LLM SANS outils (streaming).

        Le gateway lit le tag [I/CF/BG] depuis le stream et décide ensuite si un
        tool_loop est nécessaire (uniquement pour CF). Pour BG, le worker fait le vrai travail.
        """
        session.add_message("user", user_message)
        system = self._build_system(notifications=notifications)
        logger.debug("Agent responding", session_id=str(session.id), stream=stream)

        result = await self._llm.complete(
            messages=session.messages,
            system=system,
            stream=True,  # toujours streaming pour la détection du tag
        )
        if not stream:
            # collecte le stream pour les appelants non-streaming (tests, consolidation…)
            chunks: list[str] = []
            async for chunk in result:  # type: ignore[union-attr]
                chunks.append(chunk)
            text = "".join(chunks)
            session.add_message("assistant", text)
            return text
        return result

    async def respond_tools(
        self,
        session: Session,
        notifications: list[str] | None = None,
    ) -> str:
        """Tool loop sur les messages existants (user déjà ajouté par respond()).

        Conservé pour rétrocompatibilité (tests). Le gateway utilise désormais
        start_routing_stream() + finalize_tool_capture().
        """
        system = self._build_system(notifications=notifications)
        return await self._llm.tool_loop(
            messages=session.messages,
            system=system,
            tools=self._tool_registry.schemas(),  # type: ignore[union-attr]
            tool_executor=self._tool_registry.call_str,  # type: ignore[union-attr]
        )

    def start_routing_stream(
        self,
        session: Session,
        user_message: str,
        notifications: list[str] | None = None,
        recall_summary: str | None = None,
    ) -> tuple[AsyncIterator[str], ToolCapture | None]:
        """Un seul appel LLM streamé, avec outils si disponibles.

        Ajoute user_message à la session, lance le stream et retourne
        (stream, capture). Le ToolCapture est populé dès que le stream
        est entièrement consommé ; None si le provider ne supporte pas les outils.
        """
        session.add_message("user", user_message)
        system = self._build_system(notifications=notifications, recall_summary=recall_summary)
        logger.debug("Agent routing stream", session_id=str(session.id))

        if self.has_tools() and hasattr(self._llm, "stream_with_capture"):
            stream, capture = self._llm.stream_with_capture(  # type: ignore[union-attr]
                messages=session.messages,
                system=system,
                tools=self._tool_registry.schemas(),  # type: ignore[union-attr]
            )
            return stream, capture

        # Repli pour un provider sans stream_with_capture : complete() est appele
        # SANS `tools`, donc le modele ne recoit aucun schema d'outil et la capture
        # rendue vaut None — aucun outil ne peut s'executer sur ce chemin. Ce n'est
        # donc PAS un simple repli de streaming. L'ancien commentaire citait ici
        # "(Ollama, Mistral)" : les deux implementent desormais stream_with_capture,
        # et c'est justement son absence cote Ollama qui rendait tous les outils
        # inertes en mode local sans le moindre message d'erreur.
        messages_snap = list(session.messages)

        async def _simple_stream() -> AsyncIterator[str]:
            result = await self._llm.complete(messages=messages_snap, system=system, stream=True)
            async for chunk in result:  # type: ignore[union-attr]
                yield chunk

        return _simple_stream(), None

    async def force_tool_call(self, user_message: str) -> list[tuple[str, str, dict]]:
        """Relance minimale quand le premier jet n'a produit AUCUN appel.

        Le prompt complet fait 22 Ko : la consigne d'outil y est noyée, et le
        modèle répond volontiers « c'est fait » sans rien déclencher. L'ablation
        montre qu'un prompt réduit au menu d'outils obtient un appel là où le
        prompt complet n'en obtient aucun — c'est ce prompt-là qu'on envoie ici,
        pour une seule question : quel appel, s'il y en a un.

        Retourne [] si aucun outil ne convient ou si la relance échoue : dans ce
        cas l'appelant garde la réponse du premier jet.
        """
        if self._tool_registry is None or not self._tool_registry.has_tools():
            return []

        menu = _compact_tool_menu(self._tool_registry.schemas())
        system = (
            "Tu es un routeur d'outils. L'utilisateur vient de demander une "
            "action. Ta seule sortie est la ligne d'appel : pas de phrase, pas "
            "d'explication, pas de bloc de code.\n\n"
            f"Outils :\n{menu}\n\n"
            "Forme attendue, sur une seule ligne :\n"
            'nom_outil(argument="valeur")\n\n'
            # AUCUN exemple avec une valeur concrete ici. Un exemple
            # « map_control(action="fly_to", location="Reykjavik") » a ete
            # ajoute le 18/09 puis retire le 19 : le modele a recopie
            # « Reykjavik » comme argument d'un AUTRE outil, et « montre moi la
            # meteo » a repondu la meteo de Reykjavik. La phrase « les arguments
            # viennent du message de l'utilisateur » ne l'en a pas empeche.
            # Les signatures du menu ci-dessus (noms d'arguments + valeurs
            # d'enum) enseignent la forme sans offrir un seul litteral copiable.
            "Toutes les valeurs viennent du message de l'utilisateur : ne copie "
            "aucune valeur venue d'ailleurs que de ce message.\n"
            "\u00c9cris UNIQUEMENT la ligne d'appel. Si et seulement si aucun "
            "outil de la liste ne peut r\u00e9pondre, \u00e9cris : AUCUN"
        )

        try:
            result = await self._llm.complete(
                messages=[{"role": "user", "content": user_message}],
                system=system,
                stream=False,
            )
        except Exception as e:
            # jrv: relance opportuniste — son échec n'est pas une panne, on
            # retombe simplement sur la réponse du premier jet. Volontairement
            # non mappé (scripts/error_audit/scan.py).
            logger.warning("Relance outil échouée", error=str(e))
            return []

        text = result if isinstance(result, str) else ""
        calls = self.extract_text_tool_calls(text)
        if not calls:
            # Sans cette trace, l'échec de la relance était MUET : le log ne
            # disait que « Aucun outil déclenché », jamais ce que le modèle
            # avait répondu. Impossible de distinguer « il a écrit AUCUN » de
            # « il a écrit un appel que l'analyseur refuse » — deux pannes aux
            # correctifs opposés. f-string volontaire : le format loguru du
            # projet n'affiche pas les kwargs.
            logger.warning(
                f"Relance outil sans appel exploitable — brut : {text.strip()[:200]!r}"
            )
        return calls

    async def execute_captured_tools(self, capture: ToolCapture) -> list[str]:
        """Exécute en parallèle les tool_use capturés et retourne les résultats bruts."""
        results = await asyncio.gather(
            *(self._tool_registry.call_str(name, inp) for _, name, inp in capture.calls)  # type: ignore[union-attr]
        )
        logger.debug("Tools executed", names=[n for _, n, _ in capture.calls])
        return list(results)

    async def synthesize(
        self,
        session: Session,
        ack_text: str,
        capture: ToolCapture,
        results: list[str],
    ) -> AsyncIterator[str]:
        """Second appel LLM pour synthétiser les résultats d'outils en réponse naturelle.

        Construit le format Anthropic tool_use/tool_result et streame la synthèse.
        """
        # Bloc assistant avec le texte d'ack + les tool_use calls
        assistant_content: list[dict] = []
        # La notation d'appel est retirée avant de rendre son texte au
        # modèle : la lui resservir comme étant sa propre parole l'encourage
        # à réécrire des appels en texte au tour suivant.
        ack_clean = self.strip_text_tool_calls(ack_text)
        if ack_clean.strip():
            assistant_content.append({"type": "text", "text": ack_clean})
        for tool_id, tool_name, tool_input in capture.calls:
            assistant_content.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            )

        # Bloc user avec les tool_result. `is_error` est le signal structuré
        # attendu par les modèles au format Anthropic ; le préfixe JRV présent
        # dans le contenu porte la même information pour les providers qui
        # aplatissent les blocs en texte (Ollama).
        tool_result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": _clip_tool_result(r),
                "is_error": _is_tool_error(r),
            }
            for (tid, _, _), r in zip(capture.calls, results, strict=True)
        ]

        messages = list(session.messages) + [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": tool_result_blocks},
        ]

        system = self._build_system()

        failures = [
            (name, result)
            for (_tid, name, _inp), result in zip(capture.calls, results, strict=True)
            if _is_tool_error(result)
        ]
        if failures:
            # Journalisé en WARNING : un échec d'outil était jusqu'ici invisible
            # partout — ni dans les logs de synthèse, ni à l'écran.
            logger.warning(
                "Outil en échec avant synthèse",
                names=[name for name, _ in failures],
                details=[result[:160] for _, result in failures],
            )
            system += (
                "\n\n## UN OUTIL VIENT D'ÉCHOUER — ne prétends pas le contraire\n\n"
                "Au moins un résultat ci-dessous est marqué en échec (préfixe "
                "`[JRV-...]`). L'action demandée N'A PAS EU LIEU. Tu dois :\n"
                "1. le dire clairement, en une phrase, dans les mots de l'utilisateur ;\n"
                "2. donner la cause concrète lisible dans le message d'erreur ;\n"
                "3. ne JAMAIS écrire « c'est lancé », « c'est fait », « voilà » ni "
                "aucune formule laissant croire que ça a marché."
            )

        logger.debug("Agent synthesizing tool results", tools=[n for _, n, _ in capture.calls])

        # Pas de tools ici : le LLM se concentre sur la synthèse, pas de chainage
        stream = await self._llm.complete(messages=messages, system=system, stream=True)
        async for chunk in stream:  # type: ignore[union-attr]
            yield chunk
