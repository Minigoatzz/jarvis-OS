# Copyright (C) 2026 Barthélemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

import asyncio
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


def _scan_balanced_parens(text: str, open_idx: int) -> str | None:
    """Retourne le contenu entre la parenthèse ouvrante et sa fermante.

    Suit l'état des guillemets : `execute_cli(command="open -a 'Safari'")` contient
    des apostrophes imbriquées, et une parenthèse peut apparaître dans une chaîne.
    Retourne None si la parenthèse n'est jamais refermée (sortie tronquée).
    """
    depth = 0
    quote: str | None = None
    for i in range(open_idx, len(text)):
        ch = text[i]
        if quote is not None:
            if ch == "\\":  # échappement : saute le caractère suivant
                continue
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
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
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _parse_call_args(args_raw: str) -> dict:
    """Parse `action="pause", level=50` en {"action": "pause", "level": 50}."""
    return {m.group(1): _coerce_arg(m.group(2)) for m in _ARG_RE.finditer(args_raw)}


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
            # Contre-instruction indispensable aux modèles locaux. Le prompt
            # statique illustre les outils par des exemples du type
            # `execute_cli(command="open -a 'Safari'")` : Claude les lit comme
            # une indication d'intention et appelle quand même nativement, un
            # modèle local de 14B les recopie littéralement en texte. Le gateway
            # ne déclenche l'exécution que sur un tool_call NATIF — la ligne
            # s'affichait donc dans la conversation sans que rien ne s'exécute
            # (observé en usage réel avec qwen3:14b sur « pause ma musique »).
            if _s.llm_provider == "local":
                dynamic_parts.append(
                    "## Comment appeler un outil — mécanisme natif OBLIGATOIRE\n\n"
                    "Les outils ci-dessus te sont fournis par le mécanisme natif de "
                    "function calling. Pour en utiliser un, ÉMETS UN APPEL D'OUTIL "
                    "NATIF.\n\n"
                    "N'écris JAMAIS l'appel en texte dans ta réponse. Écrire "
                    "`spotify_control(action=\"pause\")` n'exécute RIEN : "
                    "l'utilisateur voit cette ligne et sa musique continue.\n\n"
                    "Les notations `outil(arg=\"valeur\")` qui apparaissent ailleurs "
                    "dans ce prompt indiquent QUEL outil employer et avec quels "
                    "arguments — ce ne sont pas un format de sortie."
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

    def extract_text_tool_calls(self, text: str) -> list[tuple[str, str, dict]]:
        """Extrait les appels d'outils ÉCRITS EN TEXTE, au format du prompt statique.

        Le prompt statique définit déjà ce format — c'est son contrat, illustré
        par des exemples comme `execute_cli(command="open -a 'Safari'")` ou
        `memory_load_topic(filename="...")`. Claude l'interprète comme une
        intention et appelle nativement ; un modèle local le prend au pied de la
        lettre et écrit littéralement l'appel. Personne n'avait écrit l'analyseur
        de ce format : la ligne s'affichait dans la conversation et rien ne
        s'exécutait (confirmé en usage réel avec qwen3:14b, qui ne renvoie aucun
        tool_calls natif même en non-streaming avec les schémas dans le payload).

        Cette fonction rend donc au format texte le statut d'un vrai transport
        d'appel, à égalité avec les tool_calls natifs. Sécurité : seuls les noms
        d'outils RÉELLEMENT enregistrés sont reconnus, et les arguments sont
        passés au ToolRegistry qui les valide comme n'importe quel appel — rien
        n'est évalué comme du code.

        Retourne des tuples (id, nom, arguments) — même forme que ToolCapture.calls.
        """
        if self._tool_registry is None or not text.strip():
            return []

        names = [str(s.get("name", "")) for s in self._tool_registry.schemas()]
        found: list[tuple[int, str, dict]] = []

        for name in names:
            if not name:
                continue
            for match in re.finditer(rf"\b{re.escape(name)}\s*\(", text):
                args_raw = _scan_balanced_parens(text, match.end() - 1)
                if args_raw is None:
                    continue
                found.append((match.start(), name, _parse_call_args(args_raw)))

        # Ordre d'apparition dans le texte — un modèle peut en écrire plusieurs.
        found.sort(key=lambda item: item[0])
        return [
            (f"text_{name}_{i}", name, args) for i, (_pos, name, args) in enumerate(found)
        ]

    def mentions_tool_call_in_text(self, text: str) -> bool:
        """True si le texte contient au moins un appel d'outil écrit en toutes lettres."""
        return bool(self.extract_text_tool_calls(text))

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
        if ack_text.strip():
            assistant_content.append({"type": "text", "text": ack_text})
        for tool_id, tool_name, tool_input in capture.calls:
            assistant_content.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            )

        # Bloc user avec les tool_result
        tool_result_blocks = [
            {"type": "tool_result", "tool_use_id": tid, "content": _clip_tool_result(r)}
            for (tid, _, _), r in zip(capture.calls, results, strict=True)
        ]

        messages = list(session.messages) + [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": tool_result_blocks},
        ]

        system = self._build_system()
        logger.debug("Agent synthesizing tool results", tools=[n for _, n, _ in capture.calls])

        # Pas de tools ici : le LLM se concentre sur la synthèse, pas de chainage
        stream = await self._llm.complete(messages=messages, system=system, stream=True)
        async for chunk in stream:  # type: ignore[union-attr]
            yield chunk
