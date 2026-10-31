param([Parameter(Mandatory=$true)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Trusted .NET Framework x64 compiler unavailable' }
& $compiler /nologo /target:exe /platform:x64 /optimize+ /reference:System.Web.Extensions.dll "/out:$OutputPath" (Join-Path $PSScriptRoot 'AppContainerLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw 'AppContainer launcher compilation failed' }
