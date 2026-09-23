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

컨테이너가 켜졌으면(위 명령 성공), DB 구조도 최신으로 맞춰주세요.

```powershell
alembic upgrade head
```

이건 "DB에 새 상태값/컬럼이 추가됐어요" 같은 변경을 실제 DB에 반영하는 명령이에요. `git pull`로
코드만 받고 이걸 빼먹으면, 새 기능이 화면에서 에러로 뜰 수 있어요 — **git pull 받을 때마다
이 순서(1. Docker 켜기 → 2. alembic upgrade head)를 같이 해주세요.**

## 2. 워커 창 켜기 (파워셀 새 창)

```powershell
cd C:\Users\SSNPC\projects\mypage\backend
.venv\Scripts\Activate.ps1
celery -A app.core.celery_app worker --loglevel=info --pool=solo
```

`celery@...ready.` 라는 줄이 뜨면 성공. **이 창은 계속 켜두세요.**

## 3. 스케줄러(beat) 창 켜기 (파워셀 또 새 창) — 5분마다 자동으로 할 일 체크

```powershell
cd C:\Users\SSNPC\projects\mypage\backend
.venv\Scripts\Activate.ps1
celery -A app.core.celery_app beat --loglevel=info
```

**워커 창과는 역할이 달라요.** 워커는 "일을 실제로 처리하는 사람"이고, beat는 "5분마다 알람을
울려서 워커한테 할 일을 시키는 사람"이에요. 이 창이 꺼져 있으면 "신규 주문 자동감지"와
"쿠팡 취소/반품 자동감지"가 5분마다 자동으로 실행되지 않아요(대시보드에서 수동으로 새로고침하는
것과는 별개). **이 창도 계속 켜두세요.**

## 4. 서버 창 켜기 (파워셀 또 새 창)

```powershell
cd C:\Users\SSNPC\projects\mypage\backend
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --reload-exclude "app/static/generated/*" --port 8080
```

`Application startup complete.` 라는 줄이 뜨면 성공. **이 창도 계속 켜두세요.**

(8080, 5433, 6379 이 세 포트는 이미 확인해서 코드에 고정해둔 값이라, 보통은 그대로 잘 됩니다. 혹시라도 또 막혀있다는 에러가 나면 아래 "포트가 또 막혔을 때" 항목 참고.)

## 5. 브라우저로 접속

```
http://localhost:8080
```

(포트를 바꿨다면 8080 대신 그 번호로)

---

## 코드가 새로 바뀌었을 때 (재시작 필요)

1. 워커 창, 스케줄러(beat) 창, 서버 창 **모두** Ctrl+C로 끄기
2. 아무 창에서나 `git pull origin claude/amazing-cannon-fjkoca` → `alembic upgrade head`
3. 워커 창, 스케줄러(beat) 창, 서버 창 **모두** 위 2/3/4번 명령어로 다시 켜기
4. 브라우저 새로고침(F5)

## 자주 헷갈리는 것들

- **워커 창**, **스케줄러(beat) 창**, **서버 창**은 서로 다른 역할이에요. 워커는 "실제 일(크롤링, 이미지생성, 쿠팡등록, 취소/반품 처리)"을 하고, beat는 "5분마다 워커한테 할 일을 시키는 알람 역할"만 하고, 서버는 "브라우저 화면을 보여주는 역할"만 해요. **셋 다 켜져 있어야** 대시보드와 자동감지 기능이 정상 작동합니다.
- `git pull`은 한 곳에서 한 번만 받으면 됩니다 (같은 폴더를 보고 있어서요). 대신 재시작(Ctrl+C 후 다시 실행)은 워커/beat/서버 모두 해야 새 코드가 반영돼요.
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

## RPA(ABC마트 자동구매) 테스트하는 방법

아직 실사이트 셀렉터가 검증 전이라, 반드시 눈으로 보면서(headless 창 없이) 테스트해야 해요. 절대 돈이 안 나가는 순서로 만들어뒀습니다.

**1) 로그인 세션 저장 (한 번만, 세션 만료되면 다시)**
```powershell
cd C:\Users\SSNPC\projects\mypage\backend
.venv\Scripts\Activate.ps1
python scripts/save_abc_mart_session.py
```
브라우저 창이 뜨면 평소 쓰는 ABC마트 계정으로 로그인 → 파워셀로 돌아와서 Enter.

**2) 흐름 테스트 (결제 직전까지만, 안전)**
```powershell
python scripts/test_rpa_checkout.py <품번> <사이즈> --name 홍길동 --phone 01012345678 --addr "서울시 강남구 테헤란로 1"
```
브라우저 창이 뜨고 사이즈 선택→장바구니→배송지입력까지 자동으로 진행됩니다. 어느 단계에서 멈추거나 에러가 나면, **그 화면을 캡처**해서 보내주세요 — 선택자를 고쳐드릴게요. `--confirm-final-payment`를 붙이지 않는 한 실제 결제 버튼은 절대 누르지 않습니다.
