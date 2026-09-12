from typing import List
from fastapi import APIRouter, Depends, HTTPException, status

from mcp_orchestration.api.routes.auth import get_current_user
from mcp_orchestration.schemas.user import UserCreate, UserResponse
from mcp_orchestration.services.user_service import UserService

router = APIRouter(
    prefix="/users",
    tags=["Users"]
)

service = UserService()


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(user: UserCreate):
    """Create a new user (Alternative endpoint to register)."""
    try:
        return service.create_user(user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/", response_model=List[UserResponse])
def get_users(current_user: dict = Depends(get_current_user)):
    """Retrieve all users in the system (requires active authentication)."""
    return service.get_all_users()