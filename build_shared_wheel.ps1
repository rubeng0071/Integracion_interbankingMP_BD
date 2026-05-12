# =====================================================================
# Build + distribución del wheel `interbanking-mp-shared`.
#
# 1. Construye el wheel en ./dist/
# 2. Lo copia dentro de mp_webhook_function/ e ib_poller/ para que
#    `func azure functionapp publish` lo empaquete en el deploy.
#
# Uso:
#     .\build_shared_wheel.ps1
#     .\build_shared_wheel.ps1 -SkipBuild   # solo copia el wheel ya existente
# =====================================================================

[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$DistDir  = Join-Path $RepoRoot "dist"
$WheelGlob = "interbanking_mp_shared-*.whl"

$Components = @(
    Join-Path $RepoRoot "mp_webhook_function"
    Join-Path $RepoRoot "ib_poller"
)

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# ----- 1. Build -----
if (-not $SkipBuild) {
    Write-Step "Limpiando ./dist/"
    if (Test-Path $DistDir) {
        Remove-Item -Path $DistDir -Recurse -Force
    }

    Write-Step "Verificando que 'build' esté instalado"
    & $PythonExe -m pip install --quiet --upgrade build
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo instalar el paquete 'build' (pip exit $LASTEXITCODE)"
    }

    Write-Step "Construyendo wheel desde $RepoRoot"
    Push-Location $RepoRoot
    try {
        & $PythonExe -m build --wheel
        if ($LASTEXITCODE -ne 0) {
            throw "Build falló (exit $LASTEXITCODE)"
        }
    } finally {
        Pop-Location
    }
}

# ----- 2. Localizar el wheel ------
$Wheel = Get-ChildItem -Path $DistDir -Filter $WheelGlob -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending |
         Select-Object -First 1

if (-not $Wheel) {
    throw "No se encontró wheel en $DistDir. Corré sin -SkipBuild o verificá el build."
}

Write-Step "Wheel encontrado: $($Wheel.Name) ($([Math]::Round($Wheel.Length/1024, 1)) KB)"

# ----- 3. Distribuir a cada componente ------
foreach ($component in $Components) {
    if (-not (Test-Path $component)) {
        Write-Warning "Componente no existe todavía (se creará en Bloque 2): $component"
        continue
    }

    # Limpiar wheels viejos para evitar confusión al empaquetar.
    Get-ChildItem -Path $component -Filter $WheelGlob -ErrorAction SilentlyContinue |
        ForEach-Object {
            Write-Step "Quitando wheel viejo: $($_.FullName)"
            Remove-Item $_.FullName -Force
        }

    $dest = Join-Path $component $Wheel.Name
    Copy-Item -Path $Wheel.FullName -Destination $dest -Force
    Write-Step "Copiado a: $dest"
}

Write-Host ""
Write-Host "✅ Wheel construido y distribuido." -ForegroundColor Green
Write-Host "Próximos pasos:" -ForegroundColor Yellow
Write-Host "  cd mp_webhook_function; pip install -r requirements.txt"
Write-Host "  cd ib_poller;           pip install -r requirements.txt"
