import logging
import os
import tiktoken
from datetime import datetime, timezone
from typing import Optional
from langchain_openai import ChatOpenAI
from mcp_orchestration.core.config import settings
from mcp_orchestration.core.database import DatabaseUnavailableError, get_collection

logger = logging.getLogger("mcp_orchestration.agents.llm")


def get_llm(
    model: Optional[str] = None, temperature: float = 0.0, streaming: bool = False
) -> ChatOpenAI:
    """Instantiate a ChatOpenAI model configured to call OpenAI directly.

    If no model is specified, defaults to `settings.openai_chat_model`
    (env `OPENAI_CHAT_MODEL`, "gpt-4o-mini" out of the box - a small/cheap
    model, not the largest available). Falls back to routing through
    OpenRouter (`OPENROUTER_API`/`OPENROUTER_DEFAULT_MODEL`) if
    `OPENAI_API_KEY` isn't set, for deployments without a direct OpenAI key.
    """
    if settings.openai_api_key:
        api_key = settings.openai_api_key
        api_base = None  # ChatOpenAI's own default: real OpenAI
        model_name = model or settings.openai_chat_model
    else:
        api_key = settings.openrouter_api
        api_base = "https://openrouter.ai/api/v1"
        model_name = model or os.getenv(
            "OPENROUTER_DEFAULT_MODEL", "google/gemini-2-flash"
        )

    if not api_key:
        logger.warning(
            "Neither OPENAI_API_KEY nor OPENROUTER_API is set. "
            "Model calls will fail unless mock environment is configured."
        )
        # fallback for testing with empty key
        api_key = "dummy-key"

    return ChatOpenAI(
        model=model_name,
        temperature=temperature,
        openai_api_key=api_key,
        openai_api_base=api_base,
        streaming=streaming,
        max_tokens=7073,
    )


def estimate_tokens(text: str, model_name: str = "gpt-3.5-turbo") -> int:
    """Estimate the number of tokens in a string using tiktoken."""
    try:
        # Standard cl100k_base encoding matches most modern GPT/Gemini API tokenizers close enough
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception as e:
        logger.warning(f"Error estimating token count: {str(e)}")
        # Simple word-count division fallback
        return len(text.split()) // 4


def log_token_usage(
    user_id: str,
    session_id: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> dict:
    """Log model token execution costs and counts to MongoDB.

    Best-effort: token accounting must never break a chat request, so a
    database outage here is logged and swallowed rather than raised.
    """
    log_doc = {
        "user_id": user_id,
        "session_id": session_id,
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "timestamp": datetime.now(timezone.utc),
    }
    try:
        get_collection("token_usage").insert_one(dict(log_doc))
    except DatabaseUnavailableError as e:
        logger.warning(f"Skipped token usage logging, database unavailable: {e}")

    return log_doc
