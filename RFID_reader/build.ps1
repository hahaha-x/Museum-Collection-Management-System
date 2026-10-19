$ErrorActionPreference = 'Stop'
# Build with the Windows .NET Framework compiler, without NuGet or Java.
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Install .NET Framework 4.8 first.' }
$library = Join-Path $PSScriptRoot 'lib\AlienRFID2.dll'
if (-not (Test-Path -LiteralPath $library)) { throw 'Place the official AlienRFID2.dll in lib first.' }
$output = Join-Path $PSScriptRoot 'bin\local'
New-Item -ItemType Directory -Force -Path $output | Out-Null
$sources = Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.cs' | ForEach-Object { $_.FullName }
& $compiler /nologo /target:exe /platform:anycpu "/out:$output\RfidCollector.exe" "/reference:$library" /reference:System.Configuration.dll /reference:System.Web.Extensions.dll $sources
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Copy-Item -LiteralPath $library -Destination $output -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'App.config') -Destination (Join-Path $output 'RfidCollector.exe.config') -Force
Write-Host "Build OK: $output\RfidCollector.exe"
