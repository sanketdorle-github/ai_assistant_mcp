from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from mcp_orchestration.core.config import settings
from mcp_orchestration.core.security import create_access_token, decode_access_token
from mcp_orchestration.schemas.user import UserCreate, UserLogin, UserResponse, Token
from mcp_orchestration.services.user_service import UserService

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

user_service = UserService()

# Configures OAuth2 scheme for Swagger integration
# Since OAuth2PasswordBearer expects form data, we point it to auth/token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """FastAPI Dependency to retrieve the currently logged in user from a JWT token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception
        
    user_id: str = payload.get("sub")
    if user_id is None:
        raise credentials_exception
        
    user = user_service.get_user_by_id(user_id)
    if user is None:
        raise credentials_exception
        
    return user


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(user_in: UserCreate):
    """Register a new user."""
    try:
        user = user_service.create_user(user_in)
        return user
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/login", response_model=Token)
def login_json(credentials: UserLogin):
    """Log in with email and password via JSON payload."""
    user = user_service.authenticate_user(credentials.email, credentials.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    # The 'sub' claim should store the unique user id
    access_token = create_access_token(
        data={"sub": user["id"], "email": user["email"]},
        expires_delta=access_token_expires
    )
    
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/token", response_model=Token)
def login_form(form_data: OAuth2PasswordRequestForm = Depends()):
    """OAuth2 compatible token login, client submits form-encoded username (email) and password."""
    user = user_service.authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": user["id"], "email": user["email"]},
        expires_delta=access_token_expires
    )
    
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: dict = Depends(get_current_user)):
    """Retrieve details of the currently authenticated user."""
    return current_user
