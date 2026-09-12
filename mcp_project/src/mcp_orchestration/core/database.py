"""MongoDB connection lifecycle and collection access."""

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

from mcp_orchestration.core.config import settings


class DatabaseUnavailableError(RuntimeError):
    """Raised when MongoDB cannot be reached or authenticated."""


class Database:
    def __init__(self) -> None:
        self._client: MongoClient | None = None

    def connect(self) -> None:
        if self._client is not None:
            return

        client = MongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
        )
        try:
            client.admin.command("ping")
        except PyMongoError as error:
            client.close()
            raise DatabaseUnavailableError(
                "MongoDB connection failed. Check MONGO_URI credentials and authSource."
            ) from error
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def collection(self, name: str) -> Collection:
        if self._client is None:
            raise DatabaseUnavailableError("MongoDB has not been connected.")
        return self._client[settings.database_name][name]

    @property
    def is_connected(self) -> bool:
        return self._client is not None


database = Database()


def get_collection(name: str) -> Collection:
    """Lazily resolve a named collection. Raises if the database isn't connected yet."""
    return database.collection(name)
