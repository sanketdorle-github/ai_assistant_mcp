"""MongoDB persistence for per-user Gmail OAuth credentials.

Replaces the old shared local file (`./gmail-mcp/tokens.json`). One document
per `user_id`. `access_token`/`refresh_token` are stored already encrypted
(see `core/token_encryption.py`) - this module never encrypts/decrypts
itself, callers (auth/gmail_oauth.py, mcp/gmail_mcp_server.py) own that so
the DB layer stays a plain persistence concern, same as `repositories/users.py`.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from pymongo.errors import PyMongoError

from mcp_orchestration.core.database import Database, DatabaseUnavailableError, database


class GmailCredentialsRepository:
    def __init__(self, db: Database = database) -> None:
        self._db = db

    @property
    def _collection(self):
        return self._db.collection("gmail_credentials")

    def ensure_indexes(self) -> None:
        try:
            self._collection.create_index("user_id", unique=True)
        except PyMongoError as error:
            raise DatabaseUnavailableError(
                "Unable to prepare the gmail_credentials collection."
            ) from error

    def upsert(self, user_id: str, **fields: Any) -> None:
        """Create or replace the stored credential doc for a user."""
        try:
            self._collection.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        **fields,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except PyMongoError as error:
            raise DatabaseUnavailableError(
                "Unable to store Gmail credentials."
            ) from error

    def find_by_user_id(self, user_id: str) -> Optional[dict[str, Any]]:
        try:
            return self._collection.find_one({"user_id": user_id})
        except PyMongoError as error:
            raise DatabaseUnavailableError(
                "Unable to read Gmail credentials."
            ) from error

    def delete(self, user_id: str) -> None:
        try:
            self._collection.delete_one({"user_id": user_id})
        except PyMongoError as error:
            raise DatabaseUnavailableError(
                "Unable to delete Gmail credentials."
            ) from error
