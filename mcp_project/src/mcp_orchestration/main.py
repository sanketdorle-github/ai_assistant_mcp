from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from fastapi.middleware.cors import CORSMiddleware

from mcp_orchestration.core.logging_config import configure_logging

# Must run before any other project module (which may grab a `logging.getLogger(...)`
# and start logging at import time) is imported, so every logger ends up
# attached to the real console+file handlers instead of Python's default
# "no handlers configured" fallback.
configure_logging()
from mcp_orchestration.api.routes.chat import router as chat_router
from mcp_orchestration.api.routes.auth import router as auth_router
from mcp_orchestration.api.routes.users import router as users_router
from mcp_orchestration.api.routes.memory import router as memory_router
from mcp_orchestration.core.config import settings
from mcp_orchestration.core.database import database
from mcp_orchestration.core.middleware import RequestLoggingMiddleware
from mcp_orchestration.repositories.users import UserRepository
from mcp_orchestration.repositories.gmail_credentials import GmailCredentialsRepository

# IMPORTANT: Import the Gmail OAuth router
from mcp_orchestration.auth.gmail_oauth import router as gmail_oauth_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Own infrastructure startup and shutdown for the HTTP application."""
    if not settings.secret_key:
        raise RuntimeError("SECRET_KEY or JWT_SECRET_KEY must be configured.")
    database.connect()
    UserRepository().ensure_indexes()
    GmailCredentialsRepository().ensure_indexes()
    try:
        yield
    finally:
        database.close()


def create_app() -> FastAPI:
    """Create and configure the HTTP application."""
    app = FastAPI(
        title="MCP Orchestration API",
        description="Backend services for MCP Agent Orchestration",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS must be added before other middleware so it wraps everything,
    # including error responses - Starlette applies middleware in reverse
    # registration order.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(chat_router)
    app.include_router(memory_router)
    # IMPORTANT: Register the Gmail OAuth router
    app.include_router(gmail_oauth_router)

    @app.get("/")
    def root():
        return {"message": "MCP Orchestration API is running", "docs_url": "/docs"}

    return app


app = create_app()


def main() -> None:
    """Run the FastAPI application with Uvicorn."""
    uvicorn.run("mcp_orchestration.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
