"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates

from app.utils.logger import get_logger, setup_logging

# Initialise logging early so all modules benefit
setup_logging()
log = get_logger(__name__)

# Single Jinja2 template engine — shared across all routes
# FastAPI auto-injects 'request' when using Jinja2Templates
templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("app_starting", version="0.1.0")
    yield
    log.info("app_shutting_down")


app = FastAPI(
    title="Client Compass — Lead Generation Engine",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and register routes after app creation to avoid circular imports
from app.api import health, metrics, pages, webhooks  # noqa: E402, F401

app.include_router(pages.router)