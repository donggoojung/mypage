from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routers import orders, pipeline, products

app = FastAPI(
    title="브랜드 위탁판매/구매대행 자동화 시스템",
    description="국내 멀티 플랫폼 연계형 브랜드 위탁판매 및 구매대행 자동화 시스템 API",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.include_router(pipeline.router)
app.include_router(products.router)
app.include_router(orders.router)

# 대시보드(app/static/index.html)를 "/"에서 바로 열 수 있게 정적 파일로 서빙한다.
# API 라우터를 먼저 등록해야 "/api/..." 요청이 이 catch-all 마운트에 가로채이지 않는다.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="dashboard")
