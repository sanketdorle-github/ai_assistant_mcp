"""User business rules, independent of HTTP and MongoDB details."""

from datetime import datetime, timezone
from typing import Any

from mcp_orchestration.core.security import hash_password, verify_password
from mcp_orchestration.repositories.users import UserRepository
from mcp_orchestration.schemas.user import UserCreate


class UserService:
    def __init__(self, repository: UserRepository | None = None) -> None:
        self._repository = repository or UserRepository()

    @staticmethod
    def _serialize_user(user_doc: dict[str, Any] | None) -> dict[str, Any] | None:
        """Convert a database document to a safe API representation."""
        if user_doc is None:
            return None
        user = dict(user_doc)
        user["id"] = str(user.pop("_id"))
        user.pop("hashed_password", None)
        return user

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self._serialize_user(self._repository.find_by_email(email.lower().strip()))

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._serialize_user(self._repository.find_by_id(user_id))

    def create_user(self, user_in: UserCreate) -> dict[str, Any]:
        email = user_in.email.lower().strip()
        user = self._repository.create(
            {
                "name": user_in.name,
                "email": email,
                "hashed_password": hash_password(user_in.password),
                "created_at": datetime.now(timezone.utc),
            }
        )
        return self._serialize_user(user)  # type: ignore[return-value]

    def authenticate_user(self, email: str, password: str) -> dict[str, Any] | None:
        user = self._repository.find_by_email(email.lower().strip())
        if user is None or not verify_password(password, user["hashed_password"]):
            return None
        return self._serialize_user(user)

    def get_all_users(self) -> list[dict[str, Any]]:
        return [
            self._serialize_user(user)  # type: ignore[misc]
            for user in self._repository.list_all()
        ]
