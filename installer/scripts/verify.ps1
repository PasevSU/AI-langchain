<#
.SYNOPSIS
    Пълната проверка на инсталатора с една команда.

.DESCRIPTION
    Пуска същите проверки, които върши и CI, в същия ред и на същото място:

      1. compileall върху пакета
      2. unittest върху installer/tests
      3. ruff  (освен при -SkipLint)
      4. mypy  (освен при -SkipLint)

    Всяка стъпка се проверява по своя код на изход, а не по текст в изхода:
    „мина" значи нула. Скриптът спира на първата червена стъпка и връща
    нейния код, за да може да се ползва и от автоматизация.

    ruff и mypy се пускат от папка installer, защото там е строгата
    конфигурация (installer/ruff.toml и [tool.mypy] в installer/pyproject.toml).
    От корена на хранилището няма конфигурация и проверката би била по-слаба.

.PARAMETER SkipLint
    Пропуска ruff и mypy — за бърза проверка по време на работа.

.EXAMPLE
    pwsh -File installer/scripts/verify.ps1

.EXAMPLE
    pwsh -File installer/scripts/verify.ps1 -SkipLint
#>
[CmdletBinding()]
param(
    [switch]$SkipLint
)

$ErrorActionPreference = 'Stop'

$installerRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $installerRoot

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Pass {
    param([string]$Text)
    Write-Host "[ok] $Text" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Text)
    Write-Host "[ГРЕШКА] $Text" -ForegroundColor Red
}

# ruff и mypy са инструменти, не зависимости на пакета: пускат се през uv,
# когато го има, иначе през текущия интерпретатор.
$uv = Get-Command uv -ErrorAction SilentlyContinue

function Invoke-Tool {
    param(
        [string]$Tool,
        [string[]]$Arguments
    )
    if ($uv) {
        & $uv.Source run --no-project $Tool @Arguments
    }
    else {
        & python -m $Tool @Arguments
    }
}

Push-Location $installerRoot
try {
    Write-Step 'compileall'
    & python -m compileall -q pasevsu_installer
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "compileall върна $LASTEXITCODE"
        exit $LASTEXITCODE
    }
    Write-Pass 'compileall'

    Write-Step 'unittest'
    & python -m unittest discover -s tests -t .
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "unittest върна $LASTEXITCODE"
        exit $LASTEXITCODE
    }
    Write-Pass 'unittest'

    if (-not $SkipLint) {
        Write-Step 'ruff'
        Invoke-Tool -Tool 'ruff' -Arguments @('check', '.')
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "ruff върна $LASTEXITCODE"
            exit $LASTEXITCODE
        }
        Write-Pass 'ruff'

        Write-Step 'mypy'
        Invoke-Tool -Tool 'mypy' -Arguments @('pasevsu_installer')
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "mypy върна $LASTEXITCODE"
            exit $LASTEXITCODE
        }
        Write-Pass 'mypy'
    }
    else {
        Write-Host 'ruff и mypy са пропуснати (-SkipLint).' -ForegroundColor DarkGray
    }
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host "Проверката мина: $repoRoot" -ForegroundColor Green
exit 0
