# 보탬 켜기: 도커 -> 최신 코드 -> 데이터베이스 표 갱신 -> 서버/작업자/예약작업 -> 농산물 화면
# 사용법: 바탕화면의 [보탬 켜기] 더블클릭 (보탬_켜기.bat 가 이 파일을 실행합니다)
# 시험용 옵션: -NoPause (끝에 키 입력 안 기다림), -NoBrowser (브라우저 안 엶)
param(
    [switch]$NoPause,
    [switch]$NoBrowser
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'

$backend = Split-Path -Parent $PSScriptRoot
$root    = Split-Path -Parent $backend
$branch  = 'claude/amazing-cannon-fjkoca'
$total   = 7

function Wait-Key {
    if ($NoPause) { return }
    Write-Host ''
    Write-Host '아무 키나 누르면 이 창이 닫힙니다.'
    try { $null = [Console]::ReadKey($true) } catch { Start-Sleep -Seconds 10 }
}

function Stop-WithError([string]$message) {
    Write-Host ''
    Write-Host "[문제] $message" -ForegroundColor Red
    Wait-Key
    exit 1
}

function Step([int]$n, [string]$text) {
    Write-Host ''
    Write-Host "[$n/$total] $text" -ForegroundColor Cyan
}

# 명령을 실행하고 성공 여부(종료 코드)만 돌려줍니다. 화면에 오류가 번지지 않게 조용히 실행합니다.
function Test-Command([string]$commandLine) {
    cmd /c "$commandLine >nul 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Test-Port([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(1000) -and $client.Connected) { return $true }
        return $false
    } catch { return $false } finally { $client.Close() }
}

# 보탬이 켠 서버/작업자/예약작업 (창과 그 안의 프로그램) 만 찾습니다. 다른 프로그램은 건드리지 않습니다.
$ourPattern = 'app\.main:app|app\.core\.celery_app'
function Get-OurProcesses {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $ourPattern -and $_.ProcessId -ne $PID }
}

Write-Host '==============================' -ForegroundColor Green
Write-Host '   보탬 켜는 중입니다' -ForegroundColor Green
Write-Host '==============================' -ForegroundColor Green

# ---------------------------------------------------------------- 1. 도커
Step 1 '도커를 확인하는 중...'
if (Test-Command 'docker info') {
    Write-Host '도커가 이미 켜져 있습니다.'
} else {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
    )
    $dockerExe = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $dockerExe) { Stop-WithError '도커 데스크톱 프로그램을 찾지 못했습니다. 도커가 설치되어 있는지 확인해 주세요.' }
    # 도커가 꺼질 때 남긴 낡은 연결 파일(소켓)이 있으면 이 컴퓨터의 윈도우는 지우지도 이름을 바꾸지도 못해서
    # 도커가 켜지다가 "unexpected error"로 스스로 꺼집니다. 도커가 완전히 꺼져 있을 때만, 그 폴더의 이름을 바꿔 치웁니다.
    # (도커가 새 폴더를 만듭니다. 컨테이너·데이터베이스 자료와는 관계없습니다.)
    $dockerRunning = @(Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue).Count -gt 0
    if (-not $dockerRunning) {
        $stamp = Get-Date -Format 'MMdd-HHmmss'
        foreach ($dir in @((Join-Path $env:LOCALAPPDATA 'Docker\run'), (Join-Path $env:LOCALAPPDATA 'docker-secrets-engine'))) {
            if (Test-Path -LiteralPath $dir) {
                try {
                    Rename-Item -LiteralPath $dir -NewName ((Split-Path $dir -Leaf) + ".stale-$stamp") -ErrorAction Stop
                    Write-Host "  도커의 낡은 임시 폴더를 치웠습니다: $(Split-Path $dir -Leaf)"
                } catch { }
            }
        }
    }
    Write-Host '도커 데스크톱을 켭니다. 최대 3분 걸릴 수 있어요...'
    Start-Process -FilePath $dockerExe
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        if (Test-Command 'docker info') { $ready = $true; break }
        Write-Host '  도커가 준비되기를 기다리는 중...'
    }
    if (-not $ready) { Stop-WithError '3분이 지나도 도커가 준비되지 않았습니다. 도커 데스크톱 창을 확인해 주세요.' }
    Write-Host '도커가 준비되었습니다.'
}

# ---------------------------------------------------------------- 2. 데이터베이스·대기열
Step 2 '데이터베이스와 대기열(도커)을 켜는 중...'
Push-Location $backend
$composeOk = Test-Command 'docker compose up -d'
if (-not $composeOk) { $composeOk = Test-Command 'docker-compose up -d' }
if (-not $composeOk) { Pop-Location; Stop-WithError '데이터베이스를 켜지 못했습니다. (docker compose up -d 실패)' }
# 이 프로젝트의 데이터베이스는 5433번, 대기열(Redis)은 6379번 입니다.
$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    if ((Test-Port 5433) -and (Test-Port 6379) -and
        (Test-Command 'docker compose exec -T postgres pg_isready -U fulfillment') -and
        (Test-Command 'docker compose exec -T redis redis-cli ping')) { $ready = $true; break }
    Start-Sleep -Seconds 2
    Write-Host '  데이터베이스가 준비되기를 기다리는 중...'
}
Pop-Location
if (-not $ready) { Stop-WithError '데이터베이스나 대기열이 90초 안에 준비되지 않았습니다.' }
Write-Host '데이터베이스와 대기열이 준비되었습니다.'

# ---------------------------------------------------------------- 3. 이미 켜진 것 정리
Step 3 '이미 켜져 있는 보탬을 정리하는 중...'
$old = @(Get-OurProcesses)
if ($old.Count -eq 0) {
    Write-Host '정리할 것이 없습니다.'
} else {
    # 예약 작업이 둘 이상 돌면 쿠팡 주문 확인이 중복 실행되므로 전부 끄고 새로 하나만 켭니다.
    foreach ($p in $old) { cmd /c "taskkill /PID $($p.ProcessId) /T /F >nul 2>&1" | Out-Null }
    Start-Sleep -Seconds 2
    Write-Host "이전에 켜진 것 $($old.Count)개를 껐습니다."
}
if (Test-Port 8080) {
    Stop-WithError '8080번 접속 통로(포트)를 보탬이 아닌 다른 프로그램이 쓰고 있습니다. 그 프로그램을 끄고 다시 시도해 주세요.'
}

# ---------------------------------------------------------------- 4. 최신 코드
Step 4 '최신 코드를 받는 중...'
$current = (cmd /c "git -C `"$root`" rev-parse --abbrev-ref HEAD 2>nul").Trim()
if ($current -ne $branch) {
    Write-Host "지금 코드 갈래가 '$current' 라서 최신 코드를 받지 않고 지금 있는 코드로 켭니다."
} elseif (Test-Command "git -C `"$root`" pull origin $branch") {
    Write-Host '최신 코드를 받았습니다.'
} else {
    Write-Host '최신 코드를 받지 못했습니다. 지금 있는 코드로 켭니다.' -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 5. 데이터베이스 표 갱신
Step 5 '데이터베이스 표를 최신으로 맞추는 중...'
Push-Location $backend
& (Join-Path $backend '.venv\Scripts\python.exe') -m alembic upgrade head
$alembicCode = $LASTEXITCODE
Pop-Location
if ($alembicCode -ne 0) {
    Stop-WithError '데이터베이스 표 갱신에 실패했습니다. 위의 오류 내용을 클로드 코드에게 보여 주세요. (이 상태로는 켜지 않습니다)'
}
Write-Host '데이터베이스 표가 최신입니다.'

# ---------------------------------------------------------------- 6. 세 프로그램 켜기
Step 6 '서버, 작업자, 예약 작업 창을 켜는 중...'
$activate = ". '$backend\.venv\Scripts\Activate.ps1'"
$windows = @(
    @{ Title = '보탬 서버';     Cmd = 'uvicorn app.main:app --port 8080' },
    @{ Title = '보탬 작업자';   Cmd = 'celery -A app.core.celery_app worker --loglevel=info --pool=solo' },
    @{ Title = '보탬 예약작업'; Cmd = 'celery -A app.core.celery_app beat --loglevel=info' }
)
foreach ($w in $windows) {
    $inner = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; `$Host.UI.RawUI.WindowTitle='$($w.Title)'; Set-Location -LiteralPath '$backend'; $activate; $($w.Cmd)"
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', "`"$inner`"")
    Write-Host "  $($w.Title) 창을 열었습니다."
    Start-Sleep -Seconds 1
}

# ---------------------------------------------------------------- 7. 확인 후 브라우저
Step 7 '서버가 응답하는지 확인하는 중...'
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri 'http://localhost:8080/health' -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200 -and $r.Content -match 'ok') { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $healthy) {
    Stop-WithError '1분이 지나도 서버가 응답하지 않습니다. [보탬 서버] 창에 나온 빨간 글씨를 확인해 주세요.'
}
if (-not $NoBrowser) { Start-Process 'http://localhost:8080/agri.html' }

Write-Host ''
Write-Host '다 켜졌습니다. 창 3개는 닫지 마세요. 끌 때는 바탕화면의 [보탬 끄기]를 누르세요.' -ForegroundColor Green
Wait-Key
exit 0
