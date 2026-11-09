# Cahier de mission — revue complète de jarvis-OS (novembre 2026)

Tu es une longue session de travail sur **jarvis-OS**, l'assistant vocal personnel de
Liam (dépôt `C:\jarvis-OS`). Lis ce document en entier avant toute action. Il contient le
contexte, les règles, et six phases avec un livrable chacune.

Ta valeur n'est pas le volume produit : c'est la **justesse**. Une cause prouvée vaut
mieux que dix hypothèses. Chaque affirmation de tes livrables porte une étiquette :
**PROUVÉ** (journal, test ou mesure à l'appui, cité), **PROBABLE** (indices convergents,
cités) ou **HYPOTHÈSE** (à vérifier).

---

## 1. Contexte — ce qui est déjà décidé

**Machine.** Windows, Ryzen 9 9900X, RTX 5080 (16 Go), 31 Go de RAM. Jarvis tourne en
local avec Ollama et `qwen3:14b`, `num_ctx` 32768 (dans `.env`), génération plafonnée
(`_MAX_REPLY_TOKENS` 2048, `_MAX_TOOL_TURN_TOKENS` 4096 dans `providers/llm/local.py`).

**Principes de Liam, non négociables.**
- *Addition by subtraction* : plus complexe ne veut pas dire meilleur. Toute proposition
  dit ce qu'elle **retire** autant que ce qu'elle ajoute.
- Réparer la **machine** derrière un résultat, jamais chaque résultat un par un. Quatre
  rustines sur un même symptôme sont le signe que la cause est une couche plus bas.

**Ce qu'on sait de Qwen 14B (mesuré le 8 octobre, `scripts/diag_results/`).**
- Avec le prompt actuel (consigne « ÉCRIS l'appel »), il choisit le bon outil 42 fois
  sur 45 ; sans cette consigne, 31 sur 45, et il affirme alors des actions sans appeler
  d'outil (« C'est lancé » sans rien lancer). On **garde** l'appel écrit en texte.
- C'est un bon **exécutant** (une demande, un outil) et un mauvais **chef de projet**
  (plusieurs étapes, jugement, longs documents, calculs).
- Il suit les **exemples** plutôt que les règles : un exemple mal placé dans une
  description d'outil l'égare autant qu'une mauvaise consigne.

**Architecture cible, décidée.**
| Rôle | Modèle | Accès |
|---|---|---|
| Chat, voix, actions personnelles | Qwen local | tes données, sous la politique d'accord |
| Missions personnelles | Qwen local (réflexion activable) | données personnelles, sans web |
| Missions publiques | Gemini API gratuite, puis d'autres fournisseurs | web, **sans** données personnelles ni outil irréversible |
| Qui va où | **le code**, par des règles fixes | jamais le jugement d'un modèle |

Règle de sécurité centrale : une mission ne réunit **jamais** à la fois (1) des données
privées, (2) du contenu non fiable (pages web), (3) un moyen de faire sortir de
l'information.

**Les six garanties** (la définition de « fini » pour chaque module) :
1. **Connexion** — un seul chemin, jamais silencieuse, toujours « quoi faire ».
2. **Désignation** — désigner les choses comme un humain (« le premier », « celui de X »),
   jamais d'identifiant à inventer.
3. **Politique d'action** — lire (libre) ; réversible (libre, dit ce qu'il touche, un seul
   élément par défaut) ; irréversible ou visible par d'autres (accord avec aperçu exact) ;
   impossible (permissions jamais demandées).
4. **Preuve** — aucune action affirmée sans outil exécuté ; chaque appel tracé (entrées et
   résultat, déjà dans `api.log` : lignes `Tool <nom> | entrées=… | résultat=…`).
5. **Mémoire d'action** — Jarvis sait ce qu'il vient de faire ; « annule » ne dépend pas
   de la mémoire du modèle. **N'existe pas encore.**
6. **Restitution** — court, sans markdown, résume ce que le contenu *dit*, ne confond
   jamais « rien » et « je ne sais pas ».

**État.** Gmail : garanties 1 à 4 et 6 en place, 5 manquante. Agenda : 1 seule. Missions :
moteur refondu en septembre (une seule recette finale, `docs/architecture/missions.md`).

---

## 2. Règles de la session

**Interdit, sans exception :**
- toute action réelle sur les comptes de Liam : n'appelle jamais les outils Gmail, Agenda,
  Spotify ou envoi, ne lance pas de mission qui écrit hors de `workspace/` ;
- lire, afficher ou recopier une valeur de `.env`, d'un token (`config/*token*.json`,
  `config/google_credentials.json`) ou d'une clé : tu peux vérifier qu'une clé **existe**,
  jamais ce qu'elle **vaut** ;
- modifier `.env`, `config/approvals.json` ou les fichiers de `memory_data/` ;
- toucher la branche `main`, pousser vers un dépôt distant, réécrire l'historique ;
- supprimer des données de Liam (sessions, projets, journaux).

**Code** — seulement en phase 6, sur la branche `revue/2026-11` :
- un commit par correctif, avec ses tests, message terminé par la ligne d'attribution ;
- les fichiers sont en **CRLF** : préserve-les (vérifie après chaque écriture) ;
- chaque `except` porte un marqueur `collector.` ou `# jrv:` (`scripts/error_audit/scan.py --check`) ;
- les tests n'écrivent **jamais** dans le vrai `workspace/projects` (isole
  `project_store.WORKSPACE_DIR`) ;
- un test qui prouve un correctif **échoue sur l'ancien code par son comportement**,
  pas par une erreur d'import : vérifie-le sur `git archive HEAD`.

**Barrières, état de référence** (à retrouver identiques, ou mieux, après chaque commit) :
- tests : `PYTHONPATH=src pytest tests` — **10 échecs connus**, rien d'autre :
  `test_capability_engine.py` (3), `test_dev_extensions_inert.py` (2),
  `test_llm_errors.py::test_llm_config_error_missing_anthropic_key`,
  `test_skill_create_tool_no_backdoor.py` (1), `test_skill_lab.py` (2),
  `test_skills.py::test_skill_create_tool_succes` ;
- `ruff check src/jarvis/` : 2 erreurs connues (`show_view.py` E501, `agent.py` B905) ;
- `scripts/error_audit/scan.py --check` : OK ;
- import-linter : 3 contrats tenus, 1 cassé connu (`mission_control` en `TYPE_CHECKING`).

**Git sur cette machine** : sans permission de suppression, une commande git laisse un
`.git/index.lock` orphelin qui bloque ensuite Liam. Demande la permission de suppression
sur `C:\jarvis-OS` **avant** ton premier commit, et vérifie qu'aucun `.lock` ne reste.

**Méthode** : preuve d'abord. Un symptôme sans journal qui le montre est une hypothèse.
Un correctif sans test qui reproduit l'incident n'est pas fait.

**Continuité** : la session est longue, ton contexte peut être résumé en cours de route.
Tiens `docs/sessions/2026-11_journal.md` à jour **à la fin de chaque phase** : phase,
statut, livrables écrits, prochaine action exacte. Si tu reprends, lis ce journal avant
toute chose.

**Questions** : si une décision appartient à Liam, ne la prends pas. Écris-la dans la
section « Questions pour Liam » du journal, avec les options et ta recommandation, et
continue avec ce qui ne dépend pas d'elle.

---

## 3. Sources de preuves

| Source | Où | Ce qu'on y trouve |
|---|---|---|
| Journal de Jarvis | dossier connecté `jarvis` (`C:\Users\lglia\AppData\Local\Temp\jarvis\api.log`) | routes, outils exécutés avec entrées et résultats, relances, garde-fous |
| Journal d'Ollama | dossier connecté `Ollama` (`server.log`) | durée de chaque requête, troncatures (`truncating input prompt`), erreurs |
| Conversations | `memory_data/sessions/*.jsonl` | ce que Liam a dit, ce que Jarvis a répondu (lecture seule) |
| Missions | `workspace/projects/proj_*/.jarvis/` (`state.json`, `logs.jsonl`, `audit.jsonl`) | étapes, outils, recettes, refus |
| Mesures | `scripts/diag_results/*.json` | taux d'appels corrects par phrase (ignore le fichier `FAUX_…`) |
| Historique | `git log` | ce qui a été corrigé et pourquoi (messages de commit) |

Le journal de Jarvis horodate en heure de Montréal ; les noms de sessions sont en UTC.

---

## 4. Les six phases

### Phase 0 — Préparer (court)
Lis ce document, `docs/architecture/missions.md`, puis survole les autres documents de
`docs/architecture/` (certains datent de juin : note ce qui est périmé). Crée la branche
`revue/2026-11`, établis les barrières de référence, crée le journal. Si aucun
environnement de test n'existe, crée un venv Python 3.12 **hors du dépôt** et installe les
dépendances du projet ; ne touche pas au venv de Liam dans le dépôt.
**Livrable** : journal créé, barrières vérifiées identiques à l'état de référence.

### Phase 1 — La carte du système (lecture seule)
Écris `docs/architecture/carte_2026-11.md` :
- les composants (gateway, routeur, agent, synthèse, outils, missions, moteur proactif,
  mémoire, voix, interface) et qui appelle qui ;
- le **trajet d'un message de chat** et celui d'une **mission**, étape par étape, avec le
  nombre d'appels au modèle et leur taille (le prompt du chat fait ~44 800 caractères
  plus ~22 000 de définitions d'outils) ;
- pour chacune des **six garanties** : où elle est assurée (fichier:ligne), où elle manque ;
- ce qui est en double, mort ou contradictoire (exemple connu : deux implémentations du
  globe) ;
- tout ce qui partage le GPU (chat, missions, proactif, consolidation et AutoDream après
  chaque message) et ce que ça coûte en attente.

### Phase 2 — Le prompt et les outils (lecture seule + mesure)
Le prompt et les descriptions d'outils **sont** les instructions de Qwen.
- `prompts/system_static.md` (411 lignes) : relève les consignes contradictoires, les
  vestiges, les exemples qui enseignent un mauvais motif (`nom(action=…)` partout, le
  `**gras**` qui contredit « jamais de markdown »), ce qui ne sert jamais. Propose une
  version réduite, en disant ce qui part et pourquoi.
- Les 29 outils : chevauchements (connus : `map_control` / `show_view`, et
  `list_emails` / `read_email` / `manage_emails`, corrigé en partie), descriptions qui
  attirent les mauvaises demandes, outils utiles au chat ou seulement aux missions.
- **Mesure, avant toute affirmation** : `scripts/diag_native_calls.py` (Ollama doit
  tourner, Jarvis éteint ; `--filtre` pour rejouer des phrases). Commence par remesurer
  « Jean Coutu » (`--variant texte --runs 5 --filtre "Jean Coutu"`), corrigé depuis la
  dernière mesure. Ajoute au script les phrases réelles qui ont échoué (phase 3) : il
  devient le corpus de régression.

**Livrable** : `docs/architecture/audit_prompt_outils.md`, avec les mesures avant/après
de toute proposition testée sur une copie du prompt (ne remplace pas le vrai).

### Phase 3 — La chasse aux bugs intermittents du modèle (preuves)
Fouille les journaux et les conversations depuis septembre. Classe chaque incident :
action affirmée sans outil · mauvais outil · argument inventé · résultat recopié ·
appel écrit au lieu d'une réponse · fuite de balise ou de markdown · troncature ·
génération sans fin · délai · mauvaise route (mission au lieu de chat, ou l'inverse).
Pour chaque classe : nombre d'occurrences (avec horodatages), cause dans le code ou le
prompt, garantie concernée, correctif **système** proposé, et si un correctif existe
déjà, s'il a tenu (des incidents après sa date ?).
**Livrable** : `docs/architecture/bugs_modele_2026-11.md`, trié par fréquence × gravité.

### Phase 4 — Le système de cartes et du globe (plan)
Fichiers : `capabilities/tools/map_control.py`, `capabilities/tools/show_view.py`,
`interfaces/api/globe.py`, `interfaces/ui/static/globe.js` (page autonome),
`interfaces/ui/static/skills/globe/view.js` (vue du skill), `mapbox-style.json`.
À établir :
- l'inventaire et les doublons (deux globes, deux outils pour « montre-moi Paris ») ;
- **sécurité** : la clé AISStream part au navigateur (`api/globe.py:223`, utilisée en
  WebSocket direct dans `globe.js`) — fuite connue à fermer, en passant par le serveur ;
  le token Mapbox doit rester un token **public** (`pk.`), jamais `sk.` ;
- **honnêteté** : Jarvis dit « je te montre Paris » même quand la carte a échoué dans le
  navigateur (le 30/09 : `undefined` à l'écran, Mapbox non chargé). Il n'existe aucun
  retour du navigateur vers le serveur : propose comment la garantie 4 s'applique ici ;
- la cible (un seul globe, un seul outil, des commandes claires) et un plan par étapes.
**Livrable** : `docs/architecture/plan_cartes.md`. Pas de code dans cette phase.

### Phase 5 — La feuille de route
Liam a déjà ordonné le travail : (A) finir Gmail — retests, garantie 5, ligne d'arrivée de
15 phrases en 3 conversations neuves (0 mensonge, 0 débordement, ≥ 13/15) ; (B) l'agenda
dans le même moule ; (C) un modèle par rôle avec la règle des données dans le code et la
gestion des quotas ; (D) le module web, isolé, réservé aux missions ; (E) les missions
road trip et plan d'étude.
Intègre tes découvertes **dans cet ordre**, sans le bouleverser sans raison écrite. Pour
chaque élément : garantie servie, critère de fin mesurable, taille, dépendances.
**Livrable** : `docs/feuille_de_route_2026-11.md`, avec les cinq prochaines actions.

### Phase 6 — Correctifs sûrs (optionnel)
Seulement ce qui remplit les quatre conditions : cause **prouvée**, changement petit,
testable, sans effet hors de sa cible. Cinq au maximum, chacun sur la branche
`revue/2026-11` avec son test (qui échoue sur l'ancien code) et des barrières à parité.
Candidats probables : la fuite de la clé AISStream, un doublon d'outil prouvé par mesure.
Ne refactorise rien de large : propose-le dans la feuille de route.

---

## 5. Fin de session

Écris `docs/sessions/2026-11_rapport.md` :
1. ce que tu as trouvé (les cinq découvertes les plus importantes, étiquetées) ;
2. ce que tu as changé (commits de la branche, tests, barrières) ;
3. ce qui reste ouvert ;
4. les questions pour Liam, avec ta recommandation pour chacune.

Ne fusionne rien dans `main` : c'est Liam qui décide, après lecture.
