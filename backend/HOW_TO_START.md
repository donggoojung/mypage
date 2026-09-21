# 대시보드 새로 시작하는 방법

컴퓨터를 껐다 켰거나, 새 코드를 받은 뒤에는 항상 이 순서대로 해주세요.

## 0. 최신 코드 받기 (파워셀 아무 창이나 1개)

```powershell
cd C:\Users\SSNPC\projects\mypage\backend
.venv\Scripts\Activate.ps1
git pull origin claude/amazing-cannon-fjkoca
```

## 1. Docker Desktop 켜기

바탕화면/시작메뉴에서 Docker Desktop 실행 → 완전히 켜질 때까지 기다리기 (고래 아이콘 안정적으로 뜰 때까지, 보통 30초~1분).

Docker Desktop이 켜졌으면, 파워셀에서 DB/Redis 컨테이너도 켜주세요.

```powershell
docker compose up -d
```

## 2. 워커 창 켜기 (파워셀 새 창)

```powershell
cd C:\Users\SSNPC\projects\mypage\backend
.venv\Scripts\Activate.ps1
celery -A app.core.celery_app worker --loglevel=info --pool=solo
```

`celery@...ready.` 라는 줄이 뜨면 성공. **이 창은 계속 켜두세요.**

## 3. 서버 창 켜기 (파워셀 또 새 창)

```powershell
cd C:\Users\SSNPC\projects\mypage\backend
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --reload-exclude "app/static/generated/*" --port 8080
```

`Application startup complete.` 라는 줄이 뜨면 성공. **이 창도 계속 켜두세요.**

(8080, 5433, 6379 이 세 포트는 이미 확인해서 코드에 고정해둔 값이라, 보통은 그대로 잘 됩니다. 혹시라도 또 막혀있다는 에러가 나면 아래 "포트가 또 막혔을 때" 항목 참고.)

## 4. 브라우저로 접속

```
http://localhost:8080
```

(포트를 바꿨다면 8080 대신 그 번호로)

---

## 코드가 새로 바뀌었을 때 (재시작 필요)

1. 워커 창, 서버 창 **둘 다** Ctrl+C로 끄기
2. 아무 창에서나 `git pull origin claude/amazing-cannon-fjkoca`
3. 워커 창, 서버 창 **둘 다** 위 2번, 3번 명령어로 다시 켜기
4. 브라우저 새로고침(F5)

## 자주 헷갈리는 것들

- **워커 창**과 **서버 창**은 서로 다른 역할이에요. 워커는 "실제 일(크롤링, 이미지생성, 쿠팡등록)"을 하고, 서버는 "브라우저 화면을 보여주는 역할"만 해요. **둘 다 켜져 있어야** 대시보드가 정상 작동합니다.
- `git pull`은 한 곳에서 한 번만 받으면 됩니다 (같은 폴더를 보고 있어서요). 대신 재시작(Ctrl+C 후 다시 실행)은 워커/서버 둘 다 해야 새 코드가 반영돼요.
- 화면에 이미지가 안 보이거나 "안돼요" 싶으면, 먼저 워커 창에 에러 로그가 있는지 확인해주세요 — 실제 작업 결과/에러가 다 거기 찍힙니다.

## 포트가 또 막혔을 때 (`WinError 10013`, `ports are not available` 같은 에러)

**왜 생기나**: Windows에 Docker Desktop/WSL이 설치되면, "Hyper-V"라는 기능이 포트 번호 구간을 통째로 자기 것으로 예약해버릴 때가 있어요. 이게 랜덤하게 넓은 범위를 먹어버려서, 원래 멀쩡했던 포트가 갑자기 막힌 것처럼 보이는 거예요. 저희 프로그램 버그가 아니라 Windows 자체의 알려진 문제입니다.

**확실한 해결법** (관리자 권한 파워셀 필요):

1. 시작 메뉴에서 "PowerShell" 검색 → **마우스 우클릭 → "관리자 권한으로 실행"**
2. 아래 2줄 입력

```powershell
net stop winnat
net start winnat
```

이러면 Windows가 포트 예약 목록을 초기화해요. 그 다음 워커/서버 창을 다시 켜보시면 대부분 해결됩니다.

그래도 안 되면, `netsh interface ipv4 show excludedportrange protocol=tcp`로 막힌 구간을 확인해서 안 걸리는 포트로 바꿔야 하는데, 이건 캡처 보내주시면 제가 코드에서 고쳐드릴게요.
