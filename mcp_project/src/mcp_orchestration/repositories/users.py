"""MongoDB persistence operations for users."""

from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

from mcp_orchestration.core.database import Database, DatabaseUnavailableError, database


class UserRepository:
    def __init__(self, db: Database = database) -> None:
        self._db = db

    @property
    def _collection(self):
        return self._db.collection("users")

    def ensure_indexes(self) -> None:
        try:
            self._collection.create_index("email", unique=True)
        except PyMongoError as error:
            raise DatabaseUnavailableError("Unable to prepare the users collection.") from error

    def find_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            return self._collection.find_one({"email": email})
        except PyMongoError as error:
            raise DatabaseUnavailableError("Unable to read the users collection.") from error

    def find_by_id(self, user_id: str) -> dict[str, Any] | None:
        try:
            return self._collection.find_one({"_id": ObjectId(user_id)})
        except (PyMongoError, ValueError):
            return None

    def create(self, user: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._collection.insert_one(user)
        except DuplicateKeyError as error:
            raise ValueError("Email already registered") from error
        except PyMongoError as error:
            raise DatabaseUnavailableError("Unable to create the user.") from error
        return {**user, "_id": result.inserted_id}

    def list_all(self) -> list[dict[str, Any]]:
        try:
            return list(self._collection.find())
        except PyMongoError as error:
            raise DatabaseUnavailableError("Unable to read the users collection.") from error
