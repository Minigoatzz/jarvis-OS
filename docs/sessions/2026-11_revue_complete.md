# Cahier de mission — revue complète de jarvis-OS (novembre 2026)

Tu es une longue session de travail sur **jarvis-OS**, l'assistant vocal personnel de
Liam (dépôt `C:\jarvis-OS`). Lis ce document en entier avant toute action. Il contient le
contexte, les règles, des pistes déjà repérées et huit phases avec un livrable chacune.
Tu n'es pas seulement un inspecteur : tu **corriges** ce que tu trouves et tu **construis**
la prochaine pièce (la garantie 5), selon les règles de la section 2.

Ta valeur n'est pas le volume produit : c'est la **justesse**. Une cause prouvée vaut
mieux que dix hypothèses. Chaque affirmation de tes livrables porte une étiquette :
**PROUVÉ** (journal, test ou mesure à l'appui, cité), **PROBABLE** (indices convergents,
cités) ou **HYPOTHÈSE** (à vérifier). Liam lira d'abord ton rapport final : chaque
découverte doit pouvoir se vérifier en une minute à partir de ce que tu cites.

---

## 1. Contexte — ce qui est déjà décidé

**Machine.** Windows, Ryzen 9 9900X, RTX 5080 (16 Go), 31 Go de RAM. Jarvis tourne en
local avec Ollama et `qwen3:14b` (~8,4 Go), `num_ctx` 32768 (dans `.env`), génération
plafonnée (`_MAX_REPLY_TOKENS` 2048, `_MAX_TOOL_TURN_TOKENS` 4096 dans
`providers/llm/local.py`).

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
  description d'outil l'égare autant qu'une mauvaise consigne (incident « Jean Coutu » :
  l'exemple « le courriel de X » dans `manage_emails` attirait les demandes de lecture).

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

**Ordre de travail décidé par Liam** (ne le bouleverse pas sans raison écrite) :
(A) finir Gmail — retests, garantie 5, ligne d'arrivée de 15 phrases en 3 conversations
neuves (0 mensonge, 0 débordement, ≥ 13/15) ; (B) l'agenda dans le même moule ; (C) un
modèle par rôle, règle des données dans le code, quotas ; (D) le module web, isolé,
réservé aux missions ; (E) les missions road trip et plan d'étude.

---

## 2. Règles de la session

**Interdit, sans exception :**
- toute action réelle sur les comptes de Liam : n'appelle jamais les outils Gmail, Agenda,
  Spotify ou envoi, ne lance pas de mission qui écrit hors de `workspace/` ;
- lire, afficher ou recopier une valeur de `.env`, d'un token (`config/*token*.json`,
  `config/google_credentials.json`) ou d'une clé : tu peux vérifier qu'une clé **existe**
  (`grep -c NOM .env`), jamais ce qu'elle **vaut** ;
- modifier `.env`, `config/approvals.json` ou les fichiers de `memory_data/` ;
- toucher la branche `main`, pousser vers un dépôt distant, réécrire l'historique ;
- supprimer des données de Liam (sessions, projets, journaux) ;
- modifier `prompts/system_static.md` ou une description d'outil avant la phase 7 : les
  essais se font sur des **copies** chargées par le script de mesure ; seul un changement
  qui a gagné sa mesure est appliqué, en phase 7, sur la branche.

**Corriger : deux voies.** Tu corriges ce que tu trouves, mais pas n'importe quand.
- **Voie rapide — à tout moment, dès que c'est prouvé.** Le bug est **déterministe**,
  sa cause est **prouvée**, un test le **reproduit** (et échoue sur l'ancien code), le
  correctif reste **dans un seul module**, et il ne touche **rien de ce qu'on mesure** :
  ni `prompts/`, ni les descriptions ou schémas d'outils, ni les réglages envoyés au
  modèle (température, `num_ctx`, `num_predict`), ni le routage, ni la politique
  d'accord. Exemples : la fuite de la clé AISStream, un cas que le parseur lit mal, un
  chemin d'erreur qui ne termine jamais le tour, une exception avalée.
- **Voie mesurée — en phase 7, après les phases 3 et 4.** Tout ce qui touche le prompt,
  les outils, les réglages du modèle, ou une **cause commune** à plusieurs bugs. Mesure
  sur le corpus avant et après ; tu ne gardes le changement que s'il améliore sa cible
  **sans** faire baisser une autre catégorie.
- **Avant chaque correctif, demande-toi** : est-ce la cause, ou le symptôme d'une cause
  une couche plus bas ? Si trois bugs partagent une cause, un seul correctif, sur la
  cause. Un correctif de voie rapide qui s'avère n'être qu'un symptôme se note dans le
  journal, et la cause passe en voie mesurée.
- **Jamais pendant une mesure** : aucun changement sous `src/` ou `prompts/` tant qu'une
  mesure tourne en arrière-plan (le script charge le code du disque). Mets le correctif
  en attente dans le journal et applique-le à la fin de la mesure. Chaque fichier de
  mesure note le commit sur lequel il a tourné.
- **Hors de portée de cette session** : un refactor large, l'unification des cartes (il
  faut l'avis de Liam), l'Agenda, Gemini, le web, et tout changement de `.env` (propose
  la ligne à Liam).

**Code** — sur la branche `revue/2026-11` (et `revue/2026-11-journal` pour la phase 8) :
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
`.git/index.lock` orphelin qui bloque ensuite Liam. Pour lire, utilise
`git --no-optional-locks`. Demande la permission de suppression sur `C:\jarvis-OS`
**avant** ton premier commit, et vérifie à la fin qu'aucun `.lock` ne reste.

**Méthode** : preuve d'abord. Un symptôme sans journal qui le montre est une hypothèse.
Un correctif sans test qui reproduit l'incident n'est pas fait. Les pistes de la
section 4 sont des **pistes**, pas des conclusions : ne te laisse pas ancrer par elles,
et dis-le si les preuves les contredisent.

**Continuité** : la session est longue, ton contexte peut être résumé en cours de route.
Tiens `docs/sessions/2026-11_journal.md` à jour **à la fin de chaque phase et avant
chaque mesure longue** : phase, statut, livrables écrits, mesures en cours (fichier de
sortie, commande), prochaine action exacte. Si tu reprends, lis ce journal avant tout.

**Questions** : si une décision appartient à Liam, ne la prends pas. Écris-la dans la
section « Questions pour Liam » du journal, avec les options et ta recommandation, et
continue avec ce qui ne dépend pas d'elle.

**Arrête-toi et écris pourquoi dans le journal** si : une barrière régresse et tu ne
comprends pas pourquoi ; une action demanderait une règle interdite ; tu découvres une
fuite de secret ou une faille de sécurité active (note-la en tête du rapport, sans
recopier le secret).

---

## 3. Sources de preuves et contraintes pratiques

| Source | Où | Ce qu'on y trouve |
|---|---|---|
| Journal de Jarvis | dossier connecté `jarvis` (`C:\Users\lglia\AppData\Local\Temp\jarvis\api.log`) | routes, outils exécutés avec entrées et résultats, relances, garde-fous |
| Journaux d'Ollama | dossier connecté `Ollama` (`server*.log`, rotation : `server-5` le plus ancien) | chargements du modèle, troncatures (`truncating input prompt`), durées, erreurs |
| Conversations | `memory_data/sessions/*.jsonl` | ce que Liam a dit, ce que Jarvis a répondu (lecture seule) |
| Missions | `workspace/projects/proj_*/.jarvis/` (`state.json`, `logs.jsonl`, `audit.jsonl`) | étapes, outils, recettes, refus |
| Mesures | `scripts/diag_results/*.json` | taux d'appels corrects par phrase (ignore le fichier `FAUX_…`) |
| Historique | `git log` | ce qui a été corrigé et pourquoi (messages de commit) |

- Le journal de Jarvis horodate en heure de Montréal ; les noms de sessions sont en UTC.
- **Les journaux d'Ollama de ce dossier s'arrêtent le 17 octobre.** Trouve où Ollama
  écrit maintenant (mise à jour, autre dossier) avant de conclure quoi que ce soit sur
  la période récente ; si tu ne trouves pas, mets-le dans les questions pour Liam.
- **Tes commandes sur l'ordinateur de Liam sont coupées après ~3 minutes.** Une mesure
  complète est plus longue : lance-la en arrière-plan avec sa sortie dans un fichier
  (sous `scripts/diag_results/`), vérifie dès la phase 0 qu'un processus d'arrière-plan
  survit entre deux commandes, et sinon découpe la mesure avec `--filtre`.
- **Le GPU est partagé.** Une mesure n'est valable que si Jarvis est éteint et que rien
  d'autre n'utilise Ollama. Vérifie avec `ollama ps` (ou l'API `/api/ps`) avant et après.
- **Parallélise** : pendant qu'une mesure tourne en arrière-plan, lis du code (phases 1
  et 4). Le GPU travaille, toi aussi.

---

## 4. Pistes déjà repérées (à confirmer ou infirmer)

1. **Température 0,7 partout, y compris pour choisir l'outil.** PROUVÉ que c'est le
   réglage : `providers/llm/local.py` lignes ~180 (réponses) et ~429 (tours d'outils).
   HYPOTHÈSE : une part des bugs « occasionnels » (mauvais outil une fois sur cinq,
   argument inventé, action affirmée) est du **tirage aléatoire**, pas un défaut de
   prompt. À mesurer (phase 3). Attention : Qwen3 sans réflexion est recommandé autour
   de 0,7 et une température trop basse peut créer des répétitions — on mesure, on ne
   présume pas. Piste d'architecture : une température basse pour le **tour d'outil**
   et l'actuelle pour la **réponse parlée**.
2. **Le modèle est rechargé très souvent.** PROUVÉ : 344 démarrages de `llama-server`
   dans `server-2.log` (29/09 → 14/10), écart médian de 30 min entre deux démarrages ;
   aucun `keep_alive` dans le code ni dans `.env`. PROBABLE : Ollama décharge le modèle
   après 5 min d'inactivité (défaut), donc le premier message après une pause paie le
   chargement de ~8,4 Go plus le préremplissage d'un prompt de ~17 500 tokens. Mesure
   ce que ça coûte en secondes (journaux), et vérifie si des délais d'attente de Jarvis
   ou de l'interface tombent pendant un chargement à froid. Note aussi les démarrages
   en `-c 16384` après le passage à 32768 : qui les demande ?
3. **Le cache du prompt.** PROUVÉ : `engine/agent.py` (~464-493) met la partie statique
   d'abord et la partie dynamique (date à la minute, rappel de sessions) ensuite — bon
   pour la réutilisation du cache. HYPOTHÈSE : la synthèse (`for_synthesis`), la
   consolidation, AutoDream et le moteur proactif envoient d'autres prompts entre deux
   messages et forcent Ollama (un seul emplacement, `-np 1`) à tout re-préremplir.
   Mesure le temps de préremplissage par appel et l'ordre des appels pour un message.
4. **Le corpus de mesure ne teste qu'un tour.** PROUVÉ : `scripts/diag_native_calls.py`
   (~ligne 164) envoie une seule phrase sans historique. Or la désignation humaine vit
   dans les **suivis** (« lis-le au complet », « le deuxième », « annule »). Rien de tout
   ça n'est mesuré aujourd'hui.
5. **Incidents passés dont il faut vérifier que la correction a tenu** (cherche des
   récidives après la date du correctif dans `git log`) : écho de `/no_think` et liste
   brute recopiée à l'écran ; bouton d'envoi bloqué après un message ; mission marquée
   réussie après un refus ; suppression de 6 courriels au lieu d'un ; archivage annoncé
   mais non fait ; « Jean Coutu » vers le mauvais outil ; génération sans fin ; prompt
   tronqué à 16 384 ; « Agenda libre » prononcé « agent libre » (côté voix) ; le globe
   affichant `undefined`.

---

## 5. Les huit phases

Ordre d'exécution : 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8, en lançant les mesures de la
phase 3 en arrière-plan dès qu'elles sont prêtes. Les correctifs de voie rapide se font
au fil des phases (hors mesure en cours). **Si le temps manque**, priorité aux livrables
des phases 4 (bugs), 6 (feuille de route) et 8 (garantie 5) : ce sont ceux qui changent
ce que Liam fera ensuite.

### Phase 0 — Préparer
- Lis ce document, `docs/architecture/missions.md`, puis survole les autres documents
  de `docs/architecture/` : note dans le journal ce qui est périmé (certains datent de
  juin, avant la refonte des missions et le module Gmail).
- Crée la branche `revue/2026-11`. Si aucun environnement de test n'existe, crée un
  venv Python 3.12 **hors du dépôt** et installe les dépendances ; ne touche pas au venv
  de Liam dans le dépôt.
- Établis les barrières de référence ; vérifie qu'Ollama répond et que Jarvis est
  éteint ; teste qu'un processus d'arrière-plan survit entre deux commandes.
- **Livrable** : journal créé, barrières identiques à l'état de référence, contraintes
  pratiques vérifiées.

### Phase 1 — La carte du système (lecture seule)
Écris `docs/architecture/carte_2026-11.md`. Elle doit répondre, avec fichier:ligne, à :
- **Composants** : gateway, routeur, agent, synthèse, outils, missions, moteur
  proactif, mémoire (consolidation, AutoDream), voix, interface. Qui appelle qui.
- **Le trajet d'un message de chat**, étape par étape : chaque appel au modèle, son
  prompt, sa taille, s'il bloque la réponse ou tourne après, et ce qui se passe s'il
  échoue. Même chose pour **une mission**.
- **L'historique** : combien de tours entrent dans le prompt, comment il est coupé, ce
  qui arrive quand prompt + historique dépassent 32 768.
- **Le parseur d'appels écrits** : que fait-il avec deux appels, un appel mal formé, un
  appel au milieu d'une phrase, des guillemets, accents, retours à la ligne ou
  parenthèses dans les arguments ?
- **Les chemins d'erreur** : chaque endroit où une exception devient un message pour
  Liam ; est-ce que l'interface reçoit **toujours** l'événement de fin de tour (un tour
  qui ne se termine jamais = bouton d'envoi bloqué) ?
- **La concurrence** : qui appelle Ollama en même temps (chat, missions, proactif,
  mémoire), comment c'est mis en file, quels délais d'attente s'appliquent.
- **Les six garanties** : un tableau garantie × module (Gmail, Agenda, fichiers,
  missions, cartes, Spotify), avec où elle est assurée et où elle manque.
- **Prêt pour la suite ?** Où se brancherait le choix d'un modèle par rôle (C) ? Où
  vivrait la règle « données personnelles = local » ? Les outils portent-ils déjà des
  métadonnées (catégorie d'accord, réversibilité) ?
- **Doublons, code mort, contradictions** (connu : deux implémentations du globe).

### Phase 2 — Agrandir le corpus de mesure
Avant d'auditer le prompt, il faut un instrument qui mesure ce qui compte.
- Étends `scripts/diag_native_calls.py` (il est dans le dépôt : tu peux le modifier,
  sur la branche) pour accepter des **cas à plusieurs tours** : un historique fixe
  (messages précédents et résultats d'outils simulés), puis la phrase testée.
- Construis un corpus d'environ **60 cas**, tirés autant que possible de vraies phrases
  de Liam trouvées dans les sessions, répartis en :
  - un outil évident (lire, lister, archiver, agenda, carte, musique) ;
  - **aucun outil** — bavardage, question générale : un appel ici est une faute ;
  - désignation humaine (« le dernier de X », « le deuxième ») ;
  - suivis à plusieurs tours (« lis-le au complet », « archive-le », « annule ») ;
  - demandes dangereuses (« supprime tout », « envoie à tout le monde ») : la réponse
    attendue est un seul élément ou une demande d'accord ;
  - chevauchements connus (`map_control` / `show_view`, les trois outils Gmail).
- Note pour chaque cas l'outil attendu **et** les arguments attendus (ou acceptables),
  et score les deux. Une mesure qui ne vérifie que le nom de l'outil cache les
  arguments inventés.
- **Livrable** : le script étendu, le corpus en fichier séparé, une première mesure de
  référence (5 passages par cas) dans `scripts/diag_results/`.

### Phase 3 — Le prompt et les outils (mesure)
Le prompt et les descriptions d'outils **sont** les instructions de Qwen.
- **Remesure d'abord « Jean Coutu »** (`--variant texte --runs 5 --filtre "Jean Coutu"`),
  corrigé depuis la dernière mesure, puis le corpus complet : c'est la référence.
- **Température** (piste 1) : rejoue le corpus à 0,7, 0,3 et 0,1, cinq passages chacun.
  Rapporte, par catégorie, le taux de réussite **et** sa variance : un cas qui réussit
  3 fois sur 5 est un bug intermittent, et c'est exactement ce que Liam vit.
- **Audit de `prompts/system_static.md`** (411 lignes) : consignes contradictoires,
  vestiges, exemples qui enseignent un mauvais motif (`nom(action=…)` partout, le
  `**gras**` qui contredit « jamais de markdown »), règles qui ne servent jamais.
- **Ablation** : sur une copie du prompt, retire une section à la fois et rejoue le
  corpus (3 passages suffisent). Une section dont le retrait ne change rien est
  candidate à la suppression ; une section dont le retrait fait chuter un score reste,
  et tu sais maintenant pourquoi elle existe. Estime la durée totale avant de lancer,
  et commence par les plus grosses sections.
- **Les 29 outils** : chevauchements, descriptions qui attirent les mauvaises demandes,
  exemples trompeurs, outils utiles au chat ou seulement aux missions (ces derniers
  peuvent quitter le prompt du chat). Même méthode d'ablation pour les exemples.
- **Coût** : tokens du prompt avant et après chaque proposition, et temps de réponse.
- **Livrable** : `docs/architecture/audit_prompt_outils.md` : une proposition de prompt
  réduit (en copie), ce qui part et pourquoi, et les mesures avant/après.

### Phase 4 — La chasse aux bugs intermittents (preuves)
« Occasionnel » veut dire l'une de deux choses très différentes, et ton premier travail
est de les séparer pour chaque bug :
- **probabiliste** — le modèle le fait parfois, avec la même entrée. Le remède est un
  garde-fou dans le code (vérification, relance, réglage), pas un correctif du modèle ;
- **déterministe** — une entrée précise le provoque toujours (historique long, caractère
  spécial, état de l'interface, deux appels concurrents, modèle déchargé). Le remède est
  un correctif de code, et un test qui reproduit l'entrée.

Méthode :
1. **Inventaire** depuis septembre, dans `api.log`, les sessions et les missions. Classe
   chaque incident : action affirmée sans outil · mauvais outil · argument inventé ·
   résultat recopié · appel écrit au lieu d'une réponse · fuite de balise ou de markdown ·
   troncature · génération sans fin · délai ou blocage · mauvaise route (mission au lieu
   de chat, ou l'inverse). Relie chaque incident à son tour dans la session et à ses
   lignes de journal (heure de Montréal vs UTC).
2. **Rejeu** : construis un petit harnais qui reconstitue le prompt **exact** d'un tour
   fautif (prompt système de l'époque si possible via `git`, historique de la session,
   résultats d'outils) et le rejoue 10 fois. 0 sur 10 → l'entrée seule n'explique pas
   l'incident, cherche dans le contexte (concurrence, chargement, état) ; 3 sur 10 →
   probabiliste ; 10 sur 10 → déterministe, et tu as ton test.
3. **Le parseur** : récupère dans les journaux toutes les sorties brutes du modèle qui
   contenaient un appel, passe-les dans le parseur, et vérifie qu'il les lit comme
   prévu. Ajoute les formes qui le piègent en cas de test.
4. **Les causes hors modèle** : chargements à froid (piste 2), préremplissages répétés
   (piste 3), appels concurrents, délais d'attente, événement de fin de tour manquant.
5. **Les incidents passés** (piste 5) : la correction a-t-elle tenu ?

**Livrable** : `docs/architecture/bugs_modele_2026-11.md`, trié par fréquence × gravité.
Pour chaque classe : occurrences (avec horodatages), probabiliste ou déterministe (avec
le taux de rejeu), cause dans le code ou le prompt, garantie concernée, correctif
**système** proposé et ce qu'il retire.

### Phase 5 — Le système de cartes et du globe (plan)
Commence par les **usages**, pas par le code : de quoi Liam a-t-il besoin ?
- « Montre-moi X » (un lieu, en un mot) ;
- la mission road trip (E) : itinéraire, étapes, distances, à présenter sur une carte ;
- l'agenda (B) : le lieu d'un rendez-vous, le temps pour s'y rendre ;
- le globe actuel (météo, navires AIS) : est-ce un usage ou une démo ? Mets-le dans les
  questions pour Liam plutôt que de décider.

Ensuite l'existant : `capabilities/tools/map_control.py`, `capabilities/tools/show_view.py`,
`interfaces/api/globe.py`, `interfaces/ui/static/globe.js` (page autonome),
`interfaces/ui/static/skills/globe/view.js` (vue du skill), `mapbox-style.json`.
À établir :
- **l'inventaire et les doublons** : deux globes, deux outils pour « montre-moi Paris »
  (la mesure du 8 octobre acceptait l'un ou l'autre : c'est un symptôme) ;
- **sécurité** : la clé AISStream part au navigateur (`api/globe.py:223`, WebSocket direct
  dans `globe.js`) — fuite connue, à fermer en passant par le serveur ; le token Mapbox
  doit rester **public** (`pk.`), idéalement restreint par URL, jamais `sk.` ;
- **données personnelles** : géocoder l'adresse d'un rendez-vous chez un service externe,
  c'est faire sortir une donnée de l'agenda. Applique la règle des données (section 1) et
  dis ce qui peut partir, ce qui ne peut pas, et ce qui doit être demandé à Liam ;
- **honnêteté** (garantie 4) : Jarvis dit « je te montre Paris » même quand la carte a
  échoué dans le navigateur (le 30/09 : `undefined` à l'écran). Aucun retour ne remonte
  du navigateur. Conçois-en un : l'interface confirme « affiché » ou « échec » sur le
  canal existant, l'outil attend quelques secondes, et sans confirmation il dit qu'il ne
  sait pas ;
- **la cible** : un seul globe, un seul outil avec des commandes claires, ce qui est
  supprimé, et un plan par étapes, chacune avec son test.
**Livrable** : `docs/architecture/plan_cartes.md`. Pas de code dans cette phase.

### Phase 6 — La feuille de route et deux conceptions
1. **`docs/feuille_de_route_2026-11.md`** : intègre tes découvertes dans l'ordre A à E.
   Pour chaque élément : garantie servie, critère de fin mesurable, taille, dépendances.
   Termine par les **cinq prochaines actions**, assez précises pour être lancées telles
   quelles.
2. **`docs/architecture/conception_journal_actions.md`** — la garantie 5, prochaine sur
   la liste (A) : ce que le journal enregistre (les sorties de `manage_emails` donnent
   déjà de quoi défaire), où il s'accroche (le registre trace déjà chaque appel), comment
   « annule » trouve la dernière action sans le modèle, ce qui ne peut pas être annulé
   (un envoi) et comment Jarvis le dit, la durée de conservation, les tests.
3. **Évalue cette HYPOTHÈSE de Claude (session précédente)** : une seule fiche par outil
   — lit des données personnelles ? réversible ? visible par d'autres ? sort sur le
   réseau ? — pourrait servir à la fois la garantie 3 (accords), la garantie 5 (quoi
   journaliser), la règle « qui va où » (C) et l'isolement du web (D) : un mécanisme au
   lieu de quatre. Dis si c'est vrai dans ce code, ce que ça coûterait, et ce que ça
   permettrait de retirer. Si c'est faux, dis pourquoi.

### Phase 7 — Les correctifs mesurés
Maintenant que tu vois tous les bugs ensemble (phase 4) et que tu as les mesures
(phase 3), corrige les **causes**, en commençant par celle qui explique le plus
d'incidents. Candidats probables, **si** les mesures le prouvent : la température du
tour d'outil ; la réduction du prompt (sections sans effet mesuré) ; les descriptions
d'outils qui attirent les mauvaises demandes ; un doublon d'outil (sauf les cartes).
Pour chacun : mesure du corpus avant et après sur le même commit de départ, un commit
par changement, barrières à parité, et une ligne dans le journal avec le gain mesuré.
Un changement qui n'apporte pas de gain mesurable est **annulé**, même s'il « semble
plus propre ». Un réglage de `.env` (comme `keep_alive`) ne se modifie pas : propose la
ligne exacte à Liam, avec la mesure qui la justifie.

### Phase 8 — Construire la garantie 5 (journal d'actions et « annule »)
À partir de la conception de la phase 6, sur la branche `revue/2026-11-journal`, créée
depuis `revue/2026-11` au moment où tu commences (Liam fusionnera dans cet ordre).
**Verrou** : ne commence que si la conception est écrite, les barrières sont à parité et
aucune question pour Liam ne bloque la conception. Sinon, explique pourquoi dans le
journal et arrête-toi là.
- Petits commits, chacun avec ses tests ; tout se teste avec le faux transport Gmail
  des tests existants (`tests/test_gmail_tools.py`), **jamais** avec le vrai compte.
- Couvre au moins : « archive le dernier » puis « annule » ; « mets-le à la corbeille »
  (après accord) puis « annule » ; « annule » quand il n'y a rien à annuler ; « annule »
  après un envoi (impossible : Jarvis le dit clairement) ; « annule » après un
  redémarrage, si la conception le prévoit.
- Ajoute ces suivis au corpus de mesure (phase 2) et mesure que Qwen appelle bien le
  bon outil pour « annule ».
- Écris `scripts/demo_journal_actions.py` : une démo que Liam lance en une commande, sans
  toucher à son compte, qui montre la séquence action → annulation et ce que Jarvis dit.
- Livrable : la branche, ses tests, la démo, et une section dans le rapport final.

---

## 6. Fin de session

Écris `docs/sessions/2026-11_rapport.md`, court, à lire en premier :
1. les **cinq découvertes** les plus importantes, étiquetées, chacune avec la preuve à
   regarder pour la vérifier en une minute ;
2. ce que tu as changé : les correctifs de voie rapide, les correctifs mesurés (avec le
   gain), la garantie 5 (branche, tests, comment lancer la démo), les barrières ;
3. ce qui reste ouvert, et ce que tu n'as pas eu le temps de faire ;
4. les **questions pour Liam**, chacune avec les options et ta recommandation ;
5. la liste des livrables, dans l'ordre où les lire.

Ne fusionne rien dans `main` : c'est Liam qui décide, après lecture.
