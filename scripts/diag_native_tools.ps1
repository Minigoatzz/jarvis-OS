# Diagnostic : le modele local emet-il des tool_calls NATIFS avec ce prompt systeme ?
#
# Ablation a une variable. Chaque variante ajoute un ingredient au prompt
# systeme et on regarde si Ollama renvoie encore message.tool_calls (OUI) ou si
# l'appel bascule dans le canal texte (NON).
#
# Prerequis : ollama serve en cours. Jarvis peut rester eteint.
#
# Usage :
#   .\scripts\diag_native_tools.ps1
#   .\scripts\diag_native_tools.ps1 -Model qwen2.5:7b -SkipReal
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
    [string]$Model = "qwen3:14b",
    [switch]$SkipReal
)

$ErrorActionPreference = "Stop"

# ConvertTo-Json de PowerShell 5.1 leve "capacity was less than the current size"
# au-dela d'une certaine taille d'entree -- le prompt reel fait 22 Ko, d'ou le
# crash de la variante D. On passe donc par le serialiseur .NET sous-jacent en
# levant sa limite. Bonus : il echappe le non-ASCII en \uXXXX, ce qui supprime
# toute question d'encodage sur le corps de requete.
Add-Type -AssemblyName System.Web.Extensions
$script:Ser = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$script:Ser.MaxJsonLength = [int]::MaxValue
$script:Ser.RecursionLimit = 200

function Invoke-Ablation($label, $sys) {
    $msgs = @()
    if ($sys) { $msgs += @{ role = "system"; content = $sys } }
    $msgs += @{ role = "user"; content = "pause ma musique" }

    $body = @{
        model    = $Model
        messages = $msgs
        stream   = $false
        think    = $false
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
        Write-Host ("{0,-46} ERREUR : {1}" -f $label, $_.Exception.Message) -ForegroundColor Red
        return
    }

    if ($r.message.tool_calls) {
        $names = ($r.message.tool_calls | ForEach-Object { $_.function.name }) -join ", "
        Write-Host ("{0,-46} TOOL_CALLS: OUI  ({1})" -f $label, $names) -ForegroundColor Green
    } else {
        $txt = ($r.message.content -replace "\s+", " ").Trim()
        if ($txt.Length -gt 100) { $txt = $txt.Substring(0, 100) + "..." }
        Write-Host ("{0,-46} TOOL_CALLS: NON" -f $label) -ForegroundColor Yellow
        Write-Host ("{0,-46}   -> {1}" -f "", $txt) -ForegroundColor DarkGray
    }
}

# --- Variantes synthetiques (inchangees depuis la derniere campagne) ---------

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
        Write-Host ("{0,-46} INTROUVABLE : {1}" -f $label, $path) -ForegroundColor Red
        return
    }
    Invoke-Ablation $label (Get-Content $path -Raw -Encoding UTF8)
}

# --- Campagne ---------------------------------------------------------------

Write-Host ""
Write-Host ("  Ablation - tool_calls natifs ({0})" -f $Model) -ForegroundColor Cyan
Write-Host ""

Invoke-Ablation "A  aucun prompt systeme (temoin)"        $null
Invoke-Ablation "B  regle du tag seule"                   $tag
Invoke-Ablation "C  tag + exemples en notation texte"     $examples
Invoke-Ablation "E  C + contre-instruction"               $counter
Invoke-Ablation "F  tag + exemples sans notation"         $noNotation
Invoke-Ablation "G  tag + menu d'outils, zero exemple"    $menu

if (-not $SkipReal) {
    Write-Host ""
    Invoke-RealVariant "D  prompt reel complet (22 Ko)"       "real.txt"
    Invoke-RealVariant "D2 prompt reel, notation -> prose"    "real_args.txt"
    Invoke-RealVariant "D3 prompt reel, prose sans arguments" "real_noargs.txt"
}

Write-Host ""
Write-Host "  C vs F : meme contenu, seule la notation change." -ForegroundColor DarkGray
Write-Host "  D vs D2/D3 : est-ce que le correctif tient sur le vrai prompt ?" -ForegroundColor DarkGray
Write-Host ""
