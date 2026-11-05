# 보탬 끄기: 서버, 작업자, 예약 작업과 그 창들을 끕니다.
# 도커의 데이터베이스는 끄지도 지우지도 않습니다.
param(
    [switch]$NoPause
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'

function Wait-Key {
    if ($NoPause) { return }
    Write-Host ''
    Write-Host '아무 키나 누르면 이 창이 닫힙니다.'
    try { $null = [Console]::ReadKey($true) } catch { Start-Sleep -Seconds 10 }
}

$ourPattern = 'app\.main:app|app\.core\.celery_app'
Write-Host '보탬을 끄는 중입니다...' -ForegroundColor Cyan

$targets = @(Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $ourPattern -and $_.ProcessId -ne $PID })

if ($targets.Count -eq 0) {
    Write-Host '켜져 있는 보탬이 없습니다.'
} else {
    foreach ($p in $targets) {
        # 창과 그 안에서 돌던 프로그램까지 한꺼번에 끕니다 (/T). 이미 꺼진 것은 조용히 넘어갑니다.
        cmd /c "taskkill /PID $($p.ProcessId) /T /F >nul 2>&1" | Out-Null
    }
    Start-Sleep -Seconds 2
    $left = @(Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $ourPattern -and $_.ProcessId -ne $PID })
    if ($left.Count -gt 0) {
        Write-Host "일부($($left.Count)개)가 아직 남아 있습니다. 한 번 더 눌러 보세요." -ForegroundColor Yellow
    } else {
        Write-Host '서버, 작업자, 예약 작업을 모두 껐습니다.' -ForegroundColor Green
    }
}
Write-Host '데이터베이스(도커)는 그대로 켜 두었습니다. 자료는 안전합니다.'
Wait-Key
exit 0
