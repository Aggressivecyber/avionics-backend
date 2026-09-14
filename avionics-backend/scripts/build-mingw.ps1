param(
    [ValidateSet('Debug','Release')] [string]$Configuration = 'Debug',
    [switch]$RunTests,
    [switch]$RunDemo,
    [string]$MingwBin = 'C:\msys64\mingw64\bin'
)
$ErrorActionPreference = 'Stop'
$taskProject = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskOriginalPath = $env:PATH
$taskBuildName = 'build-gcc-' + $Configuration.ToLowerInvariant()
$taskDrive = $null
$taskMapped = $false
try {
    # Prioritize the matching runtime DLLs for gcc/cc1plus and the built binaries.
    $env:PATH = $MingwBin + ';' + $env:PATH
    $taskCmake = (Get-Command cmake.exe -ErrorAction Stop).Source
    $taskCtest = Join-Path (Split-Path $taskCmake) 'ctest.exe'
    $taskNinja = (Get-Command ninja.exe -ErrorAction Stop).Source
    $taskCompilerPath = $MingwBin.Replace('\','/')
    $taskNinja = $taskNinja.Replace('\','/')
    foreach ($taskLetter in @('V','W','X','Y','Z','U','T','S','R','Q')) {
        if (!(Test-Path -LiteralPath ($taskLetter + ':\')) -and
            !(Get-PSDrive -Name $taskLetter -ErrorAction SilentlyContinue)) {
            $taskDrive = $taskLetter + ':'
            break
        }
    }
    if (!$taskDrive) { throw 'No free drive letter for an ASCII build path.' }
    & subst.exe $taskDrive $taskProject
    if ($LASTEXITCODE -ne 0) { throw 'Could not create temporary build path.' }
    $taskMapped = $true
    $taskRoot = $taskDrive + '/'
    $taskBuild = $taskRoot + $taskBuildName
    New-Item -ItemType Directory -Path (Join-Path $taskProject $taskBuildName) -Force | Out-Null
    & $taskCmake --fresh -S $taskRoot -B $taskBuild -G Ninja "-DCMAKE_BUILD_TYPE=$Configuration" `
        "-DCMAKE_C_COMPILER=$taskCompilerPath/gcc.exe" "-DCMAKE_CXX_COMPILER=$taskCompilerPath/g++.exe" `
        "-DCMAKE_MAKE_PROGRAM=$taskNinja" 2>&1 | Tee-Object -FilePath (Join-Path $taskProject "$taskBuildName/configure.log")
    if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed.' }
    & $taskCmake --build $taskBuild --parallel 4 2>&1 | Tee-Object -FilePath (Join-Path $taskProject "$taskBuildName/build.log")
    if ($LASTEXITCODE -ne 0) { throw 'Compilation failed.' }
    if ($RunTests) {
        & $taskCtest --test-dir $taskBuild --output-on-failure --verbose 2>&1 |
            Tee-Object -FilePath (Join-Path $taskProject "$taskBuildName/test.log")
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    }
    if ($RunDemo) {
        Push-Location -LiteralPath $taskProject
        try {
            & "$taskBuild/bin/bus_backend.exe" --demo "$taskBuild/bin/bus_simulated.dll" 2>&1 |
                Tee-Object -FilePath (Join-Path $taskProject "$taskBuildName/demo.log")
            if ($LASTEXITCODE -ne 0) { throw 'Demo failed.' }
            $taskScenario = 'runs/correlation_' + [DateTime]::UtcNow.Ticks
            New-Item -ItemType Directory -Path $taskScenario | Out-Null
            & "$taskBuild/bin/bus_analyze.exe" --fixture "$taskScenario/input.avbus" 2>&1 |
                Tee-Object -FilePath (Join-Path $taskProject "$taskBuildName/analysis-demo.log")
            if ($LASTEXITCODE -ne 0) { throw 'Analysis fixture creation failed.' }
            & "$taskBuild/bin/bus_analyze.exe" --analyze config/correlation-demo.ini "$taskScenario/input.avbus" "$taskScenario/analysis" 2>&1 |
                Tee-Object -FilePath (Join-Path $taskProject "$taskBuildName/analysis-demo.log") -Append
            if ($LASTEXITCODE -ne 0) { throw 'Analysis demo failed.' }
            Write-Output "analysis_demo=$taskScenario/analysis"
        } finally { Pop-Location }
    }
} finally {
    if ($taskMapped) { & subst.exe $taskDrive /D }
    $env:PATH = $taskOriginalPath
}
