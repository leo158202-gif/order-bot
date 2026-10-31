param(
  [Parameter(Mandatory=$true)][string]$Version,
  [Parameter(Mandatory=$true)][string]$OutputPath,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [Parameter(Mandatory=$true)][string]$SitePackages,
  [Parameter(Mandatory=$true)][string]$Launcher,
  [Parameter(Mandatory=$true)][string]$SandboxLauncher,
  [Parameter(Mandatory=$true)][string]$Prerequisites,
  [Parameter(Mandatory=$true)][string]$InnoCompiler,
  [string]$BuildPython = ''
)
$ErrorActionPreference = 'Stop'
if (-not $BuildPython) { $BuildPython = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe' }
$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$stage = Join-Path $OutputPath ('OrderBot-' + $Version)
foreach ($installerName in @('MicrosoftEdgeWebview2Setup.exe','vc_redist.x64.exe')) {
  $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $Prerequisites $installerName)
  if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw "Prerequisite signature invalid: $installerName" }
}
& $BuildPython (Join-Path $PSScriptRoot 'verify_release.py') --source $sourceRoot --output $stage --version $Version --runtime $RuntimeRoot --site-packages $SitePackages --launcher $Launcher --sandbox-launcher $SandboxLauncher --prerequisites $Prerequisites
if ($LASTEXITCODE -ne 0) { throw 'Clean release preparation failed' }
& (Join-Path $stage 'runtime\python.exe') -I -B (Join-Path $stage 'bootstrap.py') --self-test
if ($LASTEXITCODE -ne 0) { throw 'Bundled standalone runtime smoke test failed' }
& $BuildPython (Join-Path $PSScriptRoot 'verify_release.py') --verify $stage
if ($LASTEXITCODE -ne 0) { throw 'Smoke test modified the release payload' }
& $InnoCompiler /Q ('/DStageRoot=' + $stage) ('/DReleaseVersion=' + $Version) ('/DOutputPath=' + $OutputPath) (Join-Path $sourceRoot 'packaging\orderbot.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
$artifact = Join-Path $OutputPath ('OrderBot-' + $Version + '-setup.exe')
$hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($artifact + '.sha256'), ($hash + '  ' + [IO.Path]::GetFileName($artifact) + "`n"))
Write-Output ('Built ' + $artifact)
Write-Output ('SHA256 ' + $hash)
