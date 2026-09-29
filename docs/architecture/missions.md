# Missions — cycle de vie, événements et points d'extension

**Date :** 2026-09-28
**Pourquoi ce document :** la mission `proj_6cf963` (« crée un fichier bonjour.txt contenant la
date du jour ») a réussi — 3 étapes sur 3 vérifiées — et l'utilisateur a conclu qu'aucune mission
n'existait. Le moteur marchait ; rien ne le disait. Ce document est la carte qui manquait : par où
passe une mission, ce qu'elle annonce, et **où intervenir** pour la faire évoluer.

---

## 1. Le trajet d'une demande

```
message utilisateur
  │
  ▼  engine/gateway.py — Gateway.handle()
  │    route = tag émis par le LLM ([I] [CF] [BG] [BG:PROJECT])
  │    puis SpeedRouter.explicit_project(message) peut la FORCER en PROJECT
  │
  ├─ route PROJECT ──► _pipe() : UNE seule branche, en tête
  │                      · répond announcements.launch_ack() (déterministe)
  │                      · n'exécute AUCUN outil, natif ou écrit en texte
  │                      · vide le flux du LLM sans l'afficher
  │
  ▼  interfaces/api/{websocket,chat,proactive}.py — après l'envoi de `done`
  │    orchestrator.launch_in_background(message, origin=...)
  │
  ▼  engine/mission/orchestrator.py — create_and_run()
  │    project_manager.create_project()
  │       LLM planificateur ──► plan_normalizer (frontière LLM → moteur)
  │    _start_worker()  — seul point de lancement (création, retry, reprise)
  │
  ▼  engine/mission/worker_agent.py — WorkerAgent.run()
       _setup_environment()
       chaque étape : _execute_step() puis Verifier.check_step()
                      (défaut OBJECTIF seulement : fichier vide, Python invalide)
       fin          : _run_acceptance() → Verifier.accept()  — LA recette
                      refus argumenté → 1 étape « Corriger d'après la recette » → recette
       finally      : sauvegarde, project_update, _announce_finished()
```

### Une seule porte : la recette

Jusqu'au 29/09, chaque étape était une porte bloquante jugée en trois couches (structure,
commande shell écrite par le planificateur, juge LLM). Mesuré sur toutes les missions :
15 refus, dont 13 venus des deux dernières couches, et les deux missions mortes avaient
un travail correct. La commande jugeait le texte du code, pas le but ; le juge ne voyait
ni les fichiers modifiés ni les supprimés et devait « refuser dans le doute ». Et avec
8 portes à 90 %, une mission réussit 43 % du temps.

Aujourd'hui :
- **Une étape est un progrès, pas un verdict.** Elle n'échoue que sur un défaut objectif,
  vrai à tout moment de la mission.
- **La recette juge la DEMANDE** d'origine, sur toutes les preuves : chaque fichier et son
  contenu, et le journal des actions (écritures, suppressions, commandes avec code et
  sortie). Elle doit citer ce qui manque pour refuser ; sans preuve de manque, elle accepte.
- Un refus argumenté ouvre UNE correction ciblée, visible dans le dashboard. Un juge en
  panne (erreur, réponse illisible) n'en ouvre pas : il n'y a rien de précis à corriger.

Ce qui a été retiré pour y arriver : l'évaluateur de commandes shell (`native_checks.py`),
le filtre à tautologies, les commandes de vérification par étape, les étapes de test et
`RAPPORT.md` injectées d'office, le rejet de plan pour critère manquant, et les deux tiers
du prompt du planificateur. 685 lignes de moins dans le moteur.

## 2. Qui décide qu'une demande devient une mission

| Mécanisme | Où | Fiabilité |
|---|---|---|
| Tag `[BG:PROJECT]` émis par le LLM | `prompts/system_static.md` | Dépend du modèle — un 14B se trompe |
| Mission demandée explicitement (« lance une mission : … ») | `router.py::_EXPLICIT_PROJECT_RE` | Déterministe |
| Livrable fichier (« crée un fichier X », « écris un script ») | `router.py::_FILE_DELIVERABLE_RE` | Déterministe |

Les deux règles déterministes appliquent ce que `system_static.md` prescrit déjà ; elles ne
l'inventent pas. Le verbe décide : **produire** → mission ; **lire, lancer, supprimer** → chat.

## 3. Événements WebSocket émis

Tous passent par `broadcast_event` → file proactive → `websocket.py::_push_proactive`.
Un élément `dict` est envoyé tel quel ; un élément **texte** est emballé en `notification`.

| Type | Émis par | Quand | Écouté par |
|---|---|---|---|
| `project_created` | orchestrator | plan validé, worker lancé | dashboard.js |
| `project_update` | worker, orchestrator | chaque changement d'étape | dashboard.js |
| `project_done` | worker | **succès uniquement** (historique, conservé) | dashboard.js |
| `project_finished` | worker `_announce_finished` | **toute** fin : done, failed, killed | — point d'extension |
| `message` (role assistant) | announcements | création, fin (avec la raison de la recette), plan inexploitable | home.js `showChannel` |
| `notification` | file proactive (texte) | éléments texte | home.js `showChannel` |

`project_finished` porte `project_id`, `title`, `status`, `files`, `reason`. **Pour réagir à la
fin d'une mission, écoute celui-là** : `project_done` ignore les échecs.

## 4. Points d'extension — « je veux… → c'est ici »

| Je veux… | Fichier | Remarque |
|---|---|---|
| Changer ce que Jarvis **dit** d'une mission | `engine/mission/announcements.py` | Fonctions pures, bornées à 160 car. (`showChannel` tronque) |
| Qu'une nouvelle formulation lance une mission | `engine/router.py` (`_EXPLICIT_PROJECT_RE`, `_FILE_DELIVERABLE_RE`) | Ajouter le cas positif ET négatif dans `test_explicit_mission_routing.py` |
| Accepter une nouvelle forme de sortie du planificateur | `engine/mission/plan_normalizer.py` | Ne jamais inventer de critère de succès |
| Changer le comportement du chat en route mission | `engine/gateway.py`, branche en tête de `_pipe()` | Une seule couture ; les branches suivantes ne voient jamais PROJECT |
| Lancer une mission depuis une nouvelle interface | `orchestrator.launch_in_background()` | Jamais `create_task(create_and_run(...))` nu |
| Changer le délai, le nettoyage ou les métriques d'un worker | `orchestrator._start_worker()` | Création, retry et reprise passent tous par là |
| Réagir à la fin d'une mission | événement `project_finished` | Ou `MissionCompleted` sur le bus interne (`events.md`) |
| Ajouter un backend d'exécution | `engine/mission/backends/` + `map_path()` | Chaque backend expose sa propre vue des chemins |
| Donner un nouvel outil au worker | `worker_agent.py` : `_WORKER_TOOLS`, `_TOOL_ACCESS_LEVEL`, `_TOOL_CATEGORY`, branche dans `_tool_executor`, ligne dans `_WORKER_SYSTEM` | Les cinq, sinon l'outil est invisible, non gardé ou non exécuté. Une action destructrice va dans une catégorie ASK |
| Changer ce qu'une étape a le droit de refuser | `quality_checker.py::_issues` | Uniquement des faits vrais à TOUT moment ; ce qui dépend de la fin de mission va dans la recette |
| Changer la recette (preuves, consigne du juge) | `verifier.py::accept` | Le juge refuse avec preuve, jamais « dans le doute » |

## 5. Invariants — à ne pas casser

1. **Route PROJECT : aucun outil du chat ne s'exécute.** La mission est l'action.
   Prouvé par `test_project_route_behaviour.py`, qui pilote réellement `Gateway.handle()`.
2. **Toute fin de mission est annoncée**, succès ou échec, avec une cause en cas d'échec.
3. **Un niveau d'accès illisible n'est jamais auto-exécuté** : il prend le premier niveau
   au-dessus de `AUTO_MAX_LEVEL`. Un nombre hors bornes est arrondi vers le haut.
4. **Un échec du planificateur est annoncé** dans la conversation, jamais perdu dans un log.
5. **Seule la recette juge si la demande est satisfaite.** Une étape n'échoue que sur un
   défaut objectif ; aucune règle par étape ne juge le texte du code.
6. **La recette voit tout ce qui s'est passé**, suppressions et sorties de commandes
   comprises, et doit citer une preuve pour refuser.
7. **Toute suppression passe par ton approbation** : `delete_file` est rattaché à la catégorie
   `file_delete` (ASK), et refusé si la gouvernance est absente.

## 6. Limites connues

- **La recette reste un LLM.** Elle voit les preuves complètes et doit les citer, mais son
  jugement reste celui du modèle. Un Retry rejoue la recette (et au besoin une correction).
- **Binaires POSIX sous Windows.** La whitelist de `worker_cli.py` (`touch`, `cat`, `ls`, `grep`…)
  vise macOS/Linux. Sous Windows ces commandes n'existent pas ; l'erreur les nomme désormais
  (`Commande introuvable : 'touch'`). Une mission qui n'écrit que via `write_file` n'est pas touchée.
- **Violation d'architecture préexistante** : `capabilities/tools/mission_control.py` importe
  `engine.mission.orchestrator` sous `TYPE_CHECKING` — annotation seule, aucun couplage à
  l'exécution, mais import-linter la compte (RÈGLE 2).
