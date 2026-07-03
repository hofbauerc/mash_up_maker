"""Mash-Up Maker backend. Run: uv run uvicorn app.main:app --reload --port 8000"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, worker
from .routers import export, library, projects, seams


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    worker.start()
    worker.enqueue_pending()  # resume analyses interrupted by a restart
    yield
    worker.shutdown()


app = FastAPI(title="Mash-Up Maker", lifespan=lifespan)

# The Vite dev server (localhost:5173) proxies /api, but allow direct calls too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(library.router)
app.include_router(projects.router)
app.include_router(seams.router)
app.include_router(export.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
