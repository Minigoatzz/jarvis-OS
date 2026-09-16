# Diagnostic : le modele local emet-il des tool_calls NATIFS avec ce prompt systeme ?
#
# Ablation. Chaque variante ajoute un ingredient au prompt systeme et on regarde
# si Ollama renvoie encore message.tool_calls (OUI) ou si l'appel bascule dans le
# canal texte (NON). La premiere variante qui passe a NON designe le coupable.
#
# Prerequis : ollama serve en cours. Jarvis peut rester eteint.
# Usage      : .\scripts\diag_native_tools.ps1

$ErrorActionPreference = "Stop"

function Test-WithSystem($label, $sys) {
    $msgs = @()
    if ($sys) { $msgs += @{ role = "system"; content = $sys } }
    $msgs += @{ role = "user"; content = "pause ma musique" }

    $body = @{
        model    = "qwen3:14b"
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

    # Encodage explicite en UTF-8 : le prompt reel fait 22 Ko de francais accentue
    # et Invoke-RestMethod ne l'encode pas correctement a partir d'une chaine.
    $json  = $body | ConvertTo-Json -Depth 10
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

    try {
        $r = Invoke-RestMethod -Uri "http://localhost:11434/api/chat" -Method Post `
                -Body $bytes -ContentType "application/json; charset=utf-8"
    } catch {
        Write-Host ("{0,-46} ERREUR : {1}" -f $label, $_.Exception.Message) -ForegroundColor Red
        return
    }

    if ($r.message.tool_calls) {
        Write-Host ("{0,-46} TOOL_CALLS: OUI" -f $label) -ForegroundColor Green
    } else {
        $txt = ($r.message.content -replace "\s+", " ")
        if ($txt.Length -gt 90) { $txt = $txt.Substring(0, 90) + "..." }
        Write-Host ("{0,-46} TOOL_CALLS: NON" -f $label) -ForegroundColor Yellow
        Write-Host ("{0,-46}   -> {1}" -f "", $txt) -ForegroundColor DarkGray
    }
}

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

$promptPath = Join-Path $PSScriptRoot "..\prompts\system_static.md"
$real = $null
if (Test-Path $promptPath) {
    $real = Get-Content $promptPath -Raw -Encoding UTF8
}

Write-Host ""
Write-Host "  Ablation - tool_calls natifs (qwen3:14b)" -ForegroundColor Cyan
Write-Host ""

Test-WithSystem "A  aucun prompt systeme (temoin)"        $null
Test-WithSystem "B  regle du tag seule"                   $tag
Test-WithSystem "C  tag + exemples en notation texte"     $examples
Test-WithSystem "E  C + contre-instruction"               $counter
Test-WithSystem "F  tag + exemples sans notation"         $noNotation
if ($real) {
    Test-WithSystem "D  prompt statique complet (22 Ko)"  $real
} else {
    Write-Host ("{0,-46} INTROUVABLE : {1}" -f "D  prompt statique complet", $promptPath) -ForegroundColor Red
}

Write-Host ""
Write-Host "  Lecture : la premiere variante en NON designe l'ingredient fautif." -ForegroundColor DarkGray
Write-Host ""
