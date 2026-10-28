param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    [ValidateSet("windows", "macos", "all")][string]$Platform = "windows",
    [ValidateSet("amd64", "arm64")][string]$Arch = "amd64",
    [ValidatePattern('^$|^\d+\.\d+\.\d+$')][string]$MinVersion = "",
    [string]$NotesFile = "CHANGELOG.md",
    [switch]$VersionOnly,
    [switch]$SkipBuild,
    [switch]$Publish
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$utf8 = [Text.UTF8Encoding]::new($false)

function Set-VersionInFile {
    param(
        [string]$RelativePath,
        [string]$Pattern,
        [string]$Replacement,
        [int]$ExpectedMatches = 1
    )
    $normalisedPath = $RelativePath -replace '[\\/]', [IO.Path]::DirectorySeparatorChar
    $path = Join-Path $projectRoot $normalisedPath
    if (-not (Test-Path -LiteralPath $path)) {
        throw "File not found: $path"
    }
    $text = [IO.File]::ReadAllText($path)
    $matches = [regex]::Matches($text, $Pattern)
    if ($matches.Count -ne $ExpectedMatches) {
        throw "Unexpected version field count in ${RelativePath}: $($matches.Count) (expected $ExpectedMatches)"
    }
    [IO.File]::WriteAllText($path, [regex]::Replace($text, $Pattern, $Replacement), $utf8)
}

Set-VersionInFile "frontend\package.json" '(?m)^(  "version": ")[^"]+("\s*,?\s*$)' "`${1}$Version`${2}"
Set-VersionInFile "frontend\package-lock.json" '(?m)^(  "version": ")[^"]+("\s*,?\s*$)' "`${1}$Version`${2}"
Set-VersionInFile "frontend\package-lock.json" '("":\s*\{\s*"name":\s*"[^"]+",\s*"version":\s*")[^"]+(")' "`${1}$Version`${2}"
Set-VersionInFile "internal\selfupdate\service.go" '(?m)^(const Version = ")[^"]+("\s*$)' "`${1}$Version`${2}"
Set-VersionInFile "build\config.yml" '(?m)^  version: "[^"]+"\r?$' "  version: `"$Version`""
Set-VersionInFile "build\windows\info.json" '("(?:file_version|product_version|ProductVersion)"\s*:\s*")[^"]+(")' "`${1}$Version`${2}" 2
Set-VersionInFile "build\windows\wails.exe.manifest" '(<assemblyIdentity\s+type="win32"\s+name="app\.nonoka\.desktop"\s+version=")[^"]+(")' "`${1}$Version`${2}"
Set-VersionInFile "build\darwin\Info.plist" '(<key>CFBundle(?:ShortVersionString|Version)</key>\s*<string>)[^<]+(</string>)' "`${1}$Version`${2}" 2
Set-VersionInFile "build\darwin\Info.dev.plist" '(<key>CFBundle(?:ShortVersionString|Version)</key>\s*<string>)[^<]+(</string>)' "`${1}$Version`${2}" 2

# Sync root pyproject.toml if present
$pyproject = Join-Path (Split-Path -Parent $projectRoot) "pyproject.toml"
if (Test-Path -LiteralPath $pyproject) {
    $text = [IO.File]::ReadAllText($pyproject)
    $pattern = '(?m)^(version\s*=\s*")[^"]+("\s*$)'
    if ([regex]::IsMatch($text, $pattern)) {
        [IO.File]::WriteAllText($pyproject, [regex]::Replace($text, $pattern, "`${1}$Version`${2}"), $utf8)
    }
}

# 只同步版本号：交给 GitHub Actions 构建时用这个
if ($VersionOnly) {
    Write-Host "Version synced to $Version"
    return
}

if (-not $SkipBuild) {
    $wails = "D:\Envir\go\bin\wails3.exe"
    if (-not (Test-Path -LiteralPath $wails)) {
        $wails = (Get-Command wails3 -ErrorAction Stop).Source
    }
    $targets = if ($Platform -eq "all") { @("windows", "macos") } else { @($Platform) }
    $originalPath = $env:Path
    $toolDirs = @((Split-Path -Parent $wails))
    $bundledNode = "D:\Envir\nodejs"
    if (Test-Path -LiteralPath $bundledNode -PathType Container) { $toolDirs += $bundledNode }
    $env:Path = ($toolDirs + $originalPath) -join [IO.Path]::PathSeparator
    Push-Location $projectRoot
    try {
        foreach ($target in $targets) {
            if ($target -eq "windows") {
                & $wails task windows:build ARCH=$Arch
            } else {
                & $wails task darwin:package:universal
            }
            if ($LASTEXITCODE -ne 0) { throw "$target build failed" }
        }
    } finally {
        Pop-Location
        $env:Path = $originalPath
    }
}

$prepare = Join-Path $PSScriptRoot "prepare-update.ps1"
& $prepare -Version $Version -Platform $Platform -Arch $Arch -MinVersion $MinVersion -NotesFile $NotesFile
if ($LASTEXITCODE -ne 0) { throw "Failed to prepare update artifacts" }

if ($Publish) {
    $node = "D:\Envir\nodejs\node.exe"
    if (-not (Test-Path -LiteralPath $node)) {
        $node = (Get-Command node -ErrorAction Stop).Source
    }
    $publisher = Join-Path $PSScriptRoot "publish-update.mjs"
    $outputDir = Join-Path (Join-Path $projectRoot "bin\update") $Version
    & $node $publisher --dir $outputDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to publish update artifacts to R2" }
}
