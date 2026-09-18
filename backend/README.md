# 브랜드 위탁판매/구매대행 자동화 시스템 — Backend

`PRD.md` (프로젝트 루트) 기획안 기준 Phase 1 (기반 인프라 + 소싱 크롤러 Mock) 산출물입니다.

## 로컬 개발 환경 준비

```bash
cd backend
poetry install          # 또는: python3.11 -m venv .venv && pip install -r 아래 패키지들
cp .env.example .env
```

PostgreSQL 16 / Redis는 `docker-compose up -d` 로 띄우거나, 로컬에 직접 설치된 서비스를 사용합니다.

## DB 마이그레이션

```bash
alembic upgrade head      # 스키마 적용
alembic downgrade base    # 전체 롤백 (ENUM 타입까지 정리됨)
```

## Celery 워커 실행

```bash
celery -A app.core.celery_app.celery_app worker --loglevel=info
```

## 테스트

```bash
pytest -v
```

`tests/conftest.py`가 개발 DB(`fulfillment`)와 분리된 `fulfillment_test` DB에 스키마를 생성/삭제하며,
테스트마다 테이블을 비워 격리합니다.

## Mock 모드

`.env`의 `USE_MOCK_*` 플래그가 `true`이면 외부 API 키 없이 로컬 개발이 가능합니다.
실제 연동 시 해당 플래그를 `false`로 바꾸고 API 키를 채워 넣으면 됩니다 (PRD 부록 체크리스트 참고).
