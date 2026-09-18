from fastapi import FastAPI

app = FastAPI(
    title="브랜드 위탁판매/구매대행 자동화 시스템",
    description="국내 멀티 플랫폼 연계형 브랜드 위탁판매 및 구매대행 자동화 시스템 API",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
