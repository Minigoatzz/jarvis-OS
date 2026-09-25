# Copyright (C) 2026 Barthelemy Houot
# This file is part of Jarvis OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Garde de CONTENU pour les fichiers executables ecrits par le worker.

Pourquoi ce module existe
-------------------------
`worker_cli.py` inspecte la *ligne de commande*. Il ne voit jamais le
*fichier*. Or `python script.py` est whiteliste : tout ce que le modele place
dans `script.py` s'execute ensuite avec les droits de l'utilisateur, hors de
portee complete du garde CLI. Un `shutil.rmtree` halluciné passe.

Ce module ferme ce trou en inspectant le contenu au moment du `write_file`,
avant que le fichier touche le disque. Il reste utile meme sous Docker :
defense en profondeur, on ne jette pas une couche parce qu'une autre existe.

Perimetre
---------
Uniquement les fichiers ecrits par le worker pendant une mission, et
uniquement les extensions que le backend sait reellement executer
(`WORKER_CLI_WHITELIST` : python, node, sh, ps1...). Le code de l'utilisateur
n'est jamais concerne.

Ce que le garde NE fait PAS
---------------------------
Ce n'est pas un analyseur statique. Il attrape les formes litterales qu'un LLM
produit quand il derape ; il ne resiste pas a une obfuscation deliberee
(`getattr(__builtins__, "e"+"val")`). Contre un adversaire motive, seule
l'isolation compte — d'ou Docker le jour ou les missions liront le web.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Extensions que le backend peut executer (cf. WORKER_CLI_WHITELIST).
_GUARDED_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".pyw", ".js", ".mjs", ".cjs", ".sh", ".bash", ".ps1", ".bat", ".cmd"}
)

# ── Formes sans usage legitime dans un script genere par une mission ─────────
# Chaque entree : (motif, explication montree au modele ET a l'utilisateur).
# L'explication compte autant que le blocage : le worker doit comprendre quoi
# ecrire a la place, sinon il reessaie la meme chose jusqu'a epuiser sa boucle.
_DESTRUCTIVE: list[tuple[str, str]] = [
    (r"\bshutil\s*\.\s*rmtree\b", "shutil.rmtree efface un arbre entier"),
    (r"\bos\s*\.\s*system\s*\(", "os.system contourne la whitelist CLI"),
    (r"\bos\s*\.\s*popen\s*\(", "os.popen contourne la whitelist CLI"),
    (r"\bimport\s+subprocess\b|\bsubprocess\s*\.", "subprocess contourne la whitelist CLI"),
    (r"\bimport\s+ctypes\b|\bctypes\s*\.", "ctypes appelle du code natif arbitraire"),
    (r"\bimport\s+_?winreg\b|\bwinreg\s*\.", "winreg modifie le registre Windows"),
    (r"\b__import__\s*\(", "__import__ dynamique masque la dependance reelle"),
    (r"(?<![\w.])eval\s*\(", "eval execute du code construit a l'execution"),
    (r"(?<![\w.])exec\s*\(", "exec execute du code construit a l'execution"),
    # Node : meme logique, node est whiteliste lui aussi.
    (r"\brequire\s*\(\s*['\"]child_process['\"]", "child_process contourne la whitelist CLI"),
    (r"\bfs\s*\.\s*rm(?:Sync|dirSync|dir)?\s*\(", "fs.rm efface des fichiers hors controle"),
    # Shell / PowerShell.
    (r"\brm\s+-[a-zA-Z]*[rf]", "rm -r / -f efface sans retour"),
    (r"Remove-Item\b[^\n]*-Recurse", "Remove-Item -Recurse efface un arbre entier"),
    (r"\bdel\s+/[sfqa]", "del /s /f efface sans retour"),
    (r"\breg\s+delete\b", "reg delete modifie le registre Windows"),
    (r"\bformat\s+[a-zA-Z]:", "format detruit un volume"),
]

# Acces reseau — refuse sauf si le projet a declare requires_network=true.
_NETWORK: list[tuple[str, str]] = [
    (r"\bimport\s+socket\b|\bsocket\s*\.\s*socket\s*\(", "socket"),
    (r"\bimport\s+requests\b|\brequests\s*\.\s*(?:get|post|put|delete|head)\s*\(", "requests"),
    (r"\bimport\s+httpx\b|\bhttpx\s*\.", "httpx"),
    (r"\bimport\s+urllib\b|\burllib\s*\.\s*request\b", "urllib"),
    (r"\bimport\s+ftplib\b", "ftplib"),
    (r"\bhttp\s*\.\s*client\b", "http.client"),
    (r"\bfetch\s*\(\s*['\"]https?://", "fetch()"),
]

# Operations touchant au systeme de fichiers — servent a qualifier un chemin.
_FS_OPERATION = re.compile(
    r"\b(?:open|remove|unlink|rmdir|mkdir|makedirs|rename|replace|copy\w*|move|"
    r"chmod|chown|write_text|write_bytes|writeFileSync|unlinkSync|Path)\b"
)

# Un litteral de chaine qui sort du workspace : chemin absolu POSIX ou Windows,
# tilde, ou remontee via "..". Qualifie seulement s'il partage sa ligne avec
# une operation fichier — sinon une simple mention dans un message serait bloquee.
_ESCAPING_PATH = re.compile(
    r"""['"](?:"""
    r"""[a-zA-Z]:[\\/]"""          # C:\ ou C:/
    r"""|\\\\[^\\]"""              # UNC \\serveur
    r"""|~[\\/]"""                 # ~/
    r"""|/(?:etc|usr|bin|sbin|var|root|home|Users|Windows|Program)\b"""
    r"""|(?:\.\.[\\/])"""          # ../
    r""")"""
)

_COMMENT_LINE = re.compile(r"^\s*(?:#|//|<#|rem\b)", re.IGNORECASE)


@dataclass(frozen=True)
class GuardVerdict:
    """Resultat de l'inspection. `allowed=False` => le write est refuse."""

    allowed: bool
    reason: str = ""


def is_guarded(path: str) -> bool:
    """Vrai si l'extension est executable par le backend, donc a inspecter."""
    lowered = path.lower()
    return any(lowered.endswith(suffix) for suffix in _GUARDED_SUFFIXES)


def inspect(path: str, content: str, *, allow_network: bool = False) -> GuardVerdict:
    """Inspecte le contenu d'un fichier executable avant ecriture.

    `allow_network` vient de `project.requires_network` : un projet qui a
    declare avoir besoin du reseau peut en faire ; les autres non.
    """
    if not is_guarded(path):
        return GuardVerdict(allowed=True)

    # Les commentaires sont ignores : un LLM documente souvent ce qu'il evite
    # de faire ("# ne pas utiliser subprocess ici"), et le bloquer pour ca
    # serait un faux positif garanti.
    code = "\n".join(
        line for line in content.splitlines() if not _COMMENT_LINE.match(line)
    )

    for pattern, explanation in _DESTRUCTIVE:
        if re.search(pattern, code):
            return GuardVerdict(
                allowed=False,
                reason=(
                    f"ECRITURE REFUSEE — {path} contient une operation interdite : "
                    f"{explanation}. Pour lancer une commande, utilise l'outil "
                    f"execute_cli, qui applique la whitelist ; n'appelle pas le "
                    f"systeme depuis le script."
                ),
            )

    if not allow_network:
        for pattern, name in _NETWORK:
            if re.search(pattern, code):
                return GuardVerdict(
                    allowed=False,
                    reason=(
                        f"ECRITURE REFUSEE — {path} utilise {name}, or ce projet a ete "
                        f"planifie avec requires_network=false. Retire l'acces reseau "
                        f"ou replanifie la mission en declarant le besoin reseau."
                    ),
                )

    for line in code.splitlines():
        if _ESCAPING_PATH.search(line) and _FS_OPERATION.search(line):
            return GuardVerdict(
                allowed=False,
                reason=(
                    f"ECRITURE REFUSEE — {path} manipule un chemin hors du workspace : "
                    f"{line.strip()[:100]}. Utilise uniquement des chemins relatifs "
                    f"au workspace de la mission."
                ),
            )

    return GuardVerdict(allowed=True)
