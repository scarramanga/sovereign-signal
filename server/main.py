from fastapi import FastAPI
from server.routes.health import router as health_router
from server.routes.sessions import router as sessions_router

app = FastAPI(title="sovereign-signal", version="0.1.0")

app.include_router(health_router)
app.include_router(sessions_router, prefix="/sessions", tags=["sessions"])

@app.get("/")
async def root():
    return {"service": "sovereign-signal", "status": "ok"}
