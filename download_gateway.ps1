<#
.SYNOPSIS
    从上游 opencode2api 仓库下载 Windows 网关二进制到本目录。

.DESCRIPTION
    本控制台（o2a_console.exe / opencode2api_gui.py）运行时需要同目录存在
    opencode2api.exe（上游 Go 网关）。本脚本只负责把官方网关拉到本地，
    **不修改、不打包、不分发上游二进制**，完全符合上游未声明许可证的合规要求。

.PARAMETER Version
    上游 Release 版本号，默认 v1.3.5（与 README 保持一致，如需更新请改这里或传参）。

.PARAMETER Arch
    架构，默认 windows_amd64。其它可选：windows_arm64。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File download_gateway.ps1
    powershell -ExecutionPolicy Bypass -File download_gateway.ps1 -Version v1.3.5
#>
param(
    [string]$Version = "v1.3.5",
    [string]$Arch = "windows_amd64"
)

$ErrorActionPreference = "Stop"
$base = Split-Path -Parent $MyInvocation.MyCommand.Definition
$asset = "opencode2api_${Version}_${Arch}.zip"
$url = "https://github.com/jasonxu114514/opencode2api/releases/download/$Version/$asset"
$zip = Join-Path $base $asset
$exe = Join-Path $base "opencode2api.exe"

Write-Host "[1/3] 下载上游网关 $Version ($Arch)…"
Write-Host "       $url"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
} catch {
    Write-Error "下载失败：$_`n请手动从上游 Releases 下载 $asset，解压出 opencode2api.exe 放到本目录。"
    exit 1
}
Write-Host "[2/3] 解压并提取 opencode2api.exe…"
Expand-Archive -Path $zip -DestinationPath $base -Force
$gw = Get-ChildItem -Path $base -Recurse -Filter "opencode2api.exe" | Select-Object -First 1
if ($gw) {
    Copy-Item -Path $gw.FullName -Destination $exe -Force
    Write-Host "      网关已就绪：$exe  ($([math]::Round((Get-Item $exe).Length/1MB, 1)) MB)"
} else {
    Write-Warning "压缩包内未找到 opencode2api.exe，请手动解压 $zip。"
}
Remove-Item -Path $zip -Force -ErrorAction SilentlyContinue

# 同时放一个 config.example.json 提示（若上游压缩包里有）
$ex = Get-ChildItem -Path $base -Recurse -Filter "config.example.json" | Select-Object -First 1
if ($ex -and -not (Test-Path (Join-Path $base "config.example.json"))) {
    Copy-Item -Path $ex.FullName -Destination (Join-Path $base "config.example.json") -Force
    Write-Host "      已复制 config.example.json（请复制为 config.json 并填写密钥）"
}

Write-Host "[3/3] 完成。现在运行 o2a_console.exe（或 start_console.bat）即可。"
