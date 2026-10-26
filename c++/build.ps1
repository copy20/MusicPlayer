# 一键构建 / 运行 / 自测
#
# 用法：
#   .\build.ps1                  # 默认用 MSVC 2019 + Qt msvc2017_64 套件
#   .\build.ps1 -Toolchain mingw # 用 MinGW（注意：项目路径含中文时 AUTOMOC 会读取失败）
#   .\build.ps1 -Run             # 构建完启动 GUI
#   .\build.ps1 -Selftest        # 构建完跑无头自测
#   .\build.ps1 -Clean           # 先删构建目录
param(
    [switch]$Debug,
    [switch]$Run,
    [switch]$Selftest,
    [switch]$UnitTest,
    [switch]$Clean,
    [ValidateSet("msvc", "mingw")]
    [string]$Toolchain = "msvc",
    [string]$QtMsvcDir = "D:\Qt\Qt5.12.9\5.12.9\msvc2017_64",
    [string]$QtMingwDir = "D:\Qt\Qt5.12.9\5.12.9\mingw73_64",
    [string]$MingwDir = "D:\Qt\Qt5.12.9\Tools\mingw730_64",
    [string]$VcVars = "D:\VS2019\Community\VC\Auxiliary\Build\vcvars64.bat",
    [string]$CMake = "C:\Program Files\CMake\bin\cmake.exe"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$cfg = if ($Debug) { "Debug" } else { "Release" }
$buildDir = if ($Toolchain -eq "msvc") {
    Join-Path $root ("build\" + $Toolchain)          # VS 生成器自己会再分 Debug/Release 子目录
} else {
    Join-Path $root ("build\" + $Toolchain + "\" + $cfg)
}

if (-not (Test-Path $CMake)) { throw "没找到 cmake：$CMake" }

if ($Clean -and (Test-Path (Join-Path $root "build\$Toolchain"))) {
    Remove-Item (Join-Path $root "build\$Toolchain") -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null

Push-Location $buildDir
try {
    if ($Toolchain -eq "msvc") {
        if (-not (Test-Path $VcVars)) { throw "没找到 vcvars64.bat：$VcVars（用 -VcVars 指定）" }
        if (-not (Test-Path (Join-Path $QtMsvcDir "lib\Qt5Widgets.lib"))) {
            throw "Qt MSVC 套件不完整：$QtMsvcDir"
        }
        & $CMake -G "Visual Studio 16 2019" -A x64 `
            -DCMAKE_PREFIX_PATH="$QtMsvcDir" `
            $root | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "cmake 配置失败" }

        & $CMake --build . --config $cfg -- -m | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "编译失败" }

        # 运行/自测需要 Qt 的 dll 在 PATH 里
        $env:PATH = (Join-Path $QtMsvcDir "bin") + ";" + $env:PATH
        $exeDir = Join-Path $buildDir $cfg
    } else {
        if (-not (Test-Path (Join-Path $MingwDir "bin\g++.exe"))) { throw "没找到 MinGW：$MingwDir" }
        if (-not (Test-Path (Join-Path $QtMingwDir "bin\qmake.exe"))) { throw "没找到 Qt：$QtMingwDir" }
        # 只把 MinGW 放进 PATH：含 sh.exe 的目录会让生成器退回 Unix Makefiles
        $env:PATH = (Join-Path $MingwDir "bin") + ";" + (Join-Path $QtMingwDir "bin") + ";" + $env:PATH
        & $CMake -G "MinGW Makefiles" `
            -DCMAKE_BUILD_TYPE=$cfg `
            -DCMAKE_PREFIX_PATH="$QtMingwDir" `
            -DCMAKE_MAKE_PROGRAM="$(Join-Path $MingwDir 'bin\mingw32-make.exe')" `
            $root | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "cmake 配置失败" }
        & $CMake --build . -- -j4 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "编译失败" }
        $exeDir = $buildDir
    }

    $guiExe = Join-Path $exeDir "miniplayer.exe"
    $consoleExe = Join-Path $exeDir "miniplayer_console.exe"
    $unitExe = Join-Path $exeDir "miniplayer_unittest.exe"
    Write-Host ""
    Write-Host "构建完成：" -ForegroundColor Green
    Write-Host "  $guiExe"
    Write-Host "  $consoleExe   (控制台版，可跑 --selftest)"
    Write-Host "  $unitExe   (纯逻辑单元测试)"

    if ($UnitTest) {
        & $unitExe
        exit $LASTEXITCODE
    }
    if ($Selftest) {
        & $consoleExe --selftest
        exit $LASTEXITCODE
    }
    if ($Run) {
        Start-Process -FilePath $guiExe -WorkingDirectory $root
    }
} finally {
    Pop-Location
}
