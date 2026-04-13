from fastapi import FastAPI
from server.routes.health import router as health_router

app = FastAPI(title="sovereign-signal", version="0.1.0")

app.include_router(health_router)

@app.get("/")
async def root():
    return {"service": "sovereign-signal", "status": "ok"}
