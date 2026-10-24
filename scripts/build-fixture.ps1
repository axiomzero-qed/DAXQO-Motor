param([string]$ToolsRoot = $env:DAXQO_ANDROID_TOOLS)
$ErrorActionPreference = 'Stop'
$motorRoot = Split-Path -Parent $PSScriptRoot
$project = Join-Path $motorRoot 'android-fixture'
$substExe = Join-Path $env:WINDIR 'System32\subst.exe'
$mappedDrives = [System.Collections.Generic.List[string]]::new()
$pushed = $false

function Get-FreeAsciiDrives([int]$Count) {
    $used = @([System.IO.DriveInfo]::GetDrives() | ForEach-Object { $_.Name.Substring(0, 1).ToUpperInvariant() })
    $available = @('R','S','T','U','V','W','X','Y','Z' | Where-Object { $_ -notin $used })
    if ($available.Count -lt $Count) { throw 'Two free temporary drive letters are required.' }
    return $available[0..($Count - 1)]
}

try {
    if (!$toolsRoot) {
        throw 'Existing DAXQO Android build tools were not found.'
    }
    $letters = Get-FreeAsciiDrives 2
    $toolDrive = "$($letters[0]):"
    $sourceDrive = "$($letters[1]):"
    & $substExe $toolDrive $toolsRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not map the Android tools path.' }
    $mappedDrives.Add($toolDrive)
    & $substExe $sourceDrive $project
    if ($LASTEXITCODE -ne 0) { throw 'Could not map the Motor fixture project path.' }
    $mappedDrives.Add($sourceDrive)

    $mappedTools = "$toolDrive\"
    $mappedProject = "$sourceDrive\"
    $env:JAVA_HOME = Join-Path $mappedTools 'temurin-17'
    $env:ANDROID_HOME = Join-Path $mappedTools 'android-sdk'
    $env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
    $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + ';' + $env:PATH
    $escapedSdk = $env:ANDROID_HOME.Replace('\', '\\')
    "sdk.dir=$escapedSdk" | Set-Content -LiteralPath (Join-Path $mappedProject 'local.properties') -Encoding ASCII

    Push-Location $mappedProject
    $pushed = $true
    $gradle = Join-Path $mappedTools 'gradle-8.10.2\bin\gradle.bat'
    & $gradle --offline --no-daemon assembleDebug lintDebug
    if ($LASTEXITCODE -ne 0) { throw "Gradle assembleDebug/lintDebug failed with exit code $LASTEXITCODE." }

    $apk = Join-Path $mappedProject 'app\build\outputs\apk\debug\app-debug.apk'
    if (!(Test-Path -LiteralPath $apk)) { throw 'Gradle did not produce the expected debug APK.' }
    $dist = Join-Path $motorRoot 'dist'
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    $destination = Join-Path $dist 'motor-fixture.apk'
    Copy-Item -LiteralPath $apk -Destination $destination -Force
    Write-Host "Built $destination"
} finally {
    if ($pushed) { Pop-Location }
    for ($index = $mappedDrives.Count - 1; $index -ge 0; $index--) {
        & $substExe $mappedDrives[$index] /D | Out-Null
    }
}
