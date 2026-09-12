"""Application configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    openrouter_api: str | None = os.getenv("OPENROUTER_API")
    mongo_uri: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    database_name: str = os.getenv("DB_NAME", "mcp_orchestration")
    mongo_server_selection_timeout_ms: int = int(
        os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000")
    )
    # Support the existing .env names while allowing explicit JWT-prefixed names.
    secret_key: str = os.getenv("JWT_SECRET_KEY") or os.getenv("SECRET_KEY", "")
    algorithm: str = os.getenv("JWT_ALGORITHM") or os.getenv("ALGORITHM", "HS256")
    access_token_expire_minutes: int = int(
        os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES")
        or os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
    )
    # Canonical browser return-target after a round-trip through a third
    # party (currently: Google's Gmail OAuth consent screen - see
    # auth/gmail_oauth.py's /callback). Distinct from cors_allowed_origins
    # below even though they're the same value by default - that list is
    # about which origins may call this API, this is about where to send
    # the user's browser back to.
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    # Long-term memory (agents/memory.py, mem0 + Qdrant). openai_api_key is
    # optional: if set, the embedder calls real OpenAI directly (known to
    # work); if unset, it falls back to OpenRouter via openrouter_api
    # above - unverified for embeddings specifically, see get_memory()'s
    # docstring.
    qdrant_host: str = os.getenv("QDRANT_HOST", "localhost")
    qdrant_port: int = int(os.getenv("QDRANT_PORT", "6333"))
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_embed_model: str = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    # Chat-completion model used directly against OpenAI (agents/llm.py's
    # get_llm(), and agents/memory.py's mem0 extraction LLM when
    # OPENAI_API_KEY is set) - a small/cheap model by default, overridable
    # per deployment.
    openai_chat_model: str = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    cors_allowed_origins: list[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv(
                "CORS_ALLOWED_ORIGINS", "http://localhost:5173"
            ).split(",")
            if origin.strip()
        ]
    )


settings = Settings()
