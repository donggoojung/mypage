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

(만약 8080번 포트도 막혀있다는 에러(`WinError 10013`)가 뜨면, 아래 명령어로 안 막힌 포트를 확인해서 `--port` 뒤 숫자를 바꿔보세요.)
```powershell
netsh interface ipv4 show excludedportrange protocol=tcp
```

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
