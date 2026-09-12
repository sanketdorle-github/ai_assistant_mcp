import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from mcp_orchestration.core.config import settings


def hash_password(password: str) -> str:
    """Hash a cleartext password using bcrypt.

    Returns the hashed password as a UTF-8 string.
    """
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a cleartext password against a bcrypt hashed password."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8")
        )
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JSON Web Token (JWT) containing the provided data.

    Optionally includes an expiration offset.
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode, 
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    return encoded_jwt


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate a JWT.

    Returns the claims dictionary if valid, or None if expired/invalid.
    """
    try:
        decoded = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
        )
        return decoded
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
