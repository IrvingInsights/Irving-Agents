"""
main.py
────────
FastAPI application entry point.

Run locally:
    uvicorn main:app --reload --port 8080

Deploy (Cloud Run / Gunicorn):
    gunicorn -k uvicorn.workers.UvicornWorker -b 0.0.0.0:$PORT main:app
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from irving.routes import history, meta, ops, run

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Irving Agents")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(meta.router)
app.include_router(run.router)
app.include_router(ops.router)
app.include_router(history.router)
