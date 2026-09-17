# Diagnostic : le modele local emet-il des tool_calls NATIFS avec ce prompt systeme ?
#
# Ablation. Chaque variante ajoute un ingredient au prompt systeme et on mesure
# la PROPORTION d'appels natifs sur N tirages. Le decodage se fait a
# temperature 0.7 -- la valeur reelle de production -- donc une seule requete par
# variante ne mesure rien : la campagne du 15/09 a vu la variante F passer de OUI
# a NON entre deux executions du meme prompt. C'est un taux qu'on cherche, pas un
# booleen.
#
# Conditions alignees sur providers/llm/local.py::_payload :
#   think = false, temperature = 0.7, num_ctx = settings.ollama_num_ctx (16384).
# Sans num_ctx explicite, Ollama applique son defaut VRAM (~4096) et TRONQUE le
# prompt reel de 22 Ko -- les variantes D ne testeraient alors pas ce qu'on croit.
#
# Prerequis : ollama serve en cours. Jarvis peut rester eteint.
#
# Usage :
#   .\scripts\diag_native_tools.ps1
#   .\scripts\diag_native_tools.ps1 -Model qwen2.5:7b -SkipReal
#   .\scripts\diag_native_tools.ps1 -Runs 10
#
#   foreach ($m in "qwen3:14b","qwen2.5:7b","qwen3:8b","mistral:7b") {
#       .\scripts\diag_native_tools.ps1 -Model $m -SkipReal
#   }
#
# Les variantes D lisent scripts\diag_prompts\*.txt :
#   real.txt         prompt statique reel, verbatim
#   real_args.txt    idem, les 23 notations nom(arg="v") reecrites en prose,
#                    arguments conserves
#   real_noargs.txt  idem, arguments retires (les schemas natifs les portent deja)

param(
    [string]$Model  = "qwen3:14b",
    [switch]$SkipReal,
    [int]$Runs      = 5,
    [int]$NumCtx    = 16384
)

$ErrorActionPreference = "Stop"

# ConvertTo-Json de PowerShell 5.1 leve "capacity was less than the current size"
# au-dela d'une certaine taille d'entree -- le prompt reel fait 22 Ko. On passe
# par le serialiseur .NET sous-jacent en levant sa limite ; il echappe aussi le
# non-ASCII en \uXXXX, ce qui supprime toute question d'encodage.
Add-Type -AssemblyName System.Web.Extensions
$script:Ser = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$script:Ser.MaxJsonLength = [int]::MaxValue
$script:Ser.RecursionLimit = 200

# Tout ce qui entre dans le corps de requete est caste en [string] : un objet
# rendu par le pipeline PowerShell arrive enveloppe dans un PSObject, et le
# serialiseur reflechit alors sur ses membres jusqu'a tomber sur des types
# Reflection -- d'ou "A circular reference was detected ... RuntimeModule".
function Invoke-Ablation($label, $sys) {
    $native = 0
    $tools  = @{}
    $misses = @{}

    for ($i = 1; $i -le $Runs; $i++) {
        $msgs = @()
        if ($sys) { $msgs += @{ role = "system"; content = [string]$sys } }
        $msgs += @{ role = "user"; content = "pause ma musique" }

        $body = @{
            model    = [string]$Model
            messages = $msgs
            stream   = $false
            think    = $false
            options  = @{ temperature = 0.7; num_ctx = $NumCtx }
            tools    = @(@{
                type     = "function"
                function = @{
                    name        = "spotify_control"
                    description = "Controle la lecture Spotify"
                    parameters  = @{
                        type       = "object"
                        properties = @{ action = @{ type = "string" } }
                        required   = @("action")
                    }
                }
            })
        }

        try {
            $json  = $script:Ser.Serialize($body)
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
            $r = Invoke-RestMethod -Uri "http://localhost:11434/api/chat" -Method Post `
                    -Body $bytes -ContentType "application/json; charset=utf-8"
        } catch {
            Write-Host ("{0,-44} ERREUR : {1}" -f $label, $_.Exception.Message) -ForegroundColor Red
            return
        }

        if ($r.message.tool_calls) {
            $native++
            foreach ($tc in $r.message.tool_calls) {
                $n = [string]$tc.function.name
                if ($tools.ContainsKey($n)) { $tools[$n]++ } else { $tools[$n] = 1 }
            }
        } else {
            $txt = ([string]$r.message.content -replace "\s+", " ").Trim()
            if ($txt -eq "")        { $txt = "(reponse vide)" }
            if ($txt.Length -gt 92) { $txt = $txt.Substring(0, 92) + "..." }
            if ($misses.ContainsKey($txt)) { $misses[$txt]++ } else { $misses[$txt] = 1 }
        }
    }

    if ($native -eq $Runs)  { $color = "Green" }
    elseif ($native -eq 0)  { $color = "Yellow" }
    else                    { $color = "DarkYellow" }

    $names = ($tools.Keys | Sort-Object) -join ", "
    if ($names) { $names = "  ($names)" }
    Write-Host ("{0,-44} natif {1}/{2}{3}" -f $label, $native, $Runs, $names) -ForegroundColor $color

    foreach ($k in ($misses.Keys | Sort-Object { -$misses[$_] })) {
        Write-Host ("{0,-44}   x{1} -> {2}" -f "", $misses[$k], $k) -ForegroundColor DarkGray
    }
}

# --- Variantes synthetiques (inchangees depuis la premiere campagne) ---------

$tag = "Tu es Jarvis. REGLE ABSOLUE : commence TOUJOURS ta reponse par un tag de routing [I], [CF] ou [BG]."

$examples = $tag + @"


Exemples :
- "Ouvre Safari" -> [CF] + execute_cli(command="open -a 'Safari'")
- "Telecharge cette video" -> [CF] + execute_cli(command="yt-dlp URL")
"@

$counter = $examples + @"


## Comment appeler un outil - mecanisme natif OBLIGATOIRE
Les outils te sont fournis par le mecanisme natif de function calling.
Pour en utiliser un, EMETS UN APPEL D'OUTIL NATIF.
N'ecris JAMAIS l'appel dans ta reponse, ni en texte ni en JSON.
Les notations outil(arg="valeur") ailleurs dans ce prompt indiquent QUEL outil
employer - ce n'est pas un format de sortie.
"@

$noNotation = $tag + @"


Exemples :
- "Ouvre Safari" -> [CF], puis utilise l'outil execute_cli
- "Telecharge cette video" -> [CF], puis utilise l'outil execute_cli
"@

# G : le menu d'outils exactement au format produit par Agent._build_system(),
# sans aucun exemple. C'est le prompt que Jarvis enverrait si on supprimait les
# exemples. execute_cli y figure volontairement : si le modele le choisit malgre
# tout, c'est que nommer les autres outils suffit a detourner sa selection.
$menu = $tag + @"


## Outils disponibles (router [CF] pour les utiliser)

- ``spotify_control`` : Controle la lecture Spotify (pause, play, next)
- ``execute_cli`` : Execute une commande shell whitelistee
- ``memory_search`` : Recherche semantique dans la memoire
"@

# --- Variantes sur le prompt reel -------------------------------------------

$promptDir = Join-Path $PSScriptRoot "diag_prompts"

function Invoke-RealVariant($label, $file) {
    $path = Join-Path $promptDir $file
    if (-not (Test-Path $path)) {
        Write-Host ("{0,-44} INTROUVABLE : {1}" -f $label, $path) -ForegroundColor Red
        return
    }
    # ReadAllText rend une String .NET nue, pas un PSObject (cf. note plus haut).
    Invoke-Ablation $label ([System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8))
}

# --- Campagne ---------------------------------------------------------------

Write-Host ""
Write-Host ("  Ablation - tool_calls natifs ({0})" -f $Model) -ForegroundColor Cyan
Write-Host ("  {0} tirages par variante, temperature 0.7, num_ctx {1}" -f $Runs, $NumCtx) -ForegroundColor DarkGray
Write-Host ""

Invoke-Ablation "A  aucun prompt systeme (temoin)"      $null
Invoke-Ablation "B  regle du tag seule"                 $tag
Invoke-Ablation "C  tag + exemples en notation texte"   $examples
Invoke-Ablation "E  C + contre-instruction"             $counter
Invoke-Ablation "F  tag + exemples sans notation"       $noNotation
Invoke-Ablation "G  tag + menu d'outils, zero exemple"  $menu

if (-not $SkipReal) {
    Write-Host ""
    Invoke-RealVariant "D  prompt reel complet (22 Ko)"       "real.txt"
    Invoke-RealVariant "D2 prompt reel, notation -> prose"    "real_args.txt"
    Invoke-RealVariant "D3 prompt reel, prose sans arguments" "real_noargs.txt"
}

Write-Host ""
Write-Host "  Lecture : c'est l'ECART entre taux qui compte, pas un OUI/NON isole." -ForegroundColor DarkGray
Write-Host ""
