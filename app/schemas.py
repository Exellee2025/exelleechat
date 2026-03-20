from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str
    password: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nickname_color: Optional[str] = "#4f8cff"


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_path: Optional[str] = None
    nickname_color: Optional[str] = "#4f8cff"
    created_at: datetime


class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nickname_color: Optional[str] = None


class ChatCreate(BaseModel):
    title: str
    is_public: bool = True
    create_password: Optional[str] = None
    chat_password: Optional[str] = None


class ChatJoinRequest(BaseModel):
    password: Optional[str] = None


class DirectChatCreate(BaseModel):
    user_id: int


class MessageCreate(BaseModel):
    content: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None


class MessageOut(BaseModel):
    id: int
    chat_id: int
    content: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    created_at: datetime
    user: UserOut


class ChatOut(BaseModel):
    id: int
    title: str
    is_direct: bool
    is_public: bool
    requires_password: bool
    joined: bool
    created_at: datetime
    members: List[UserOut] = []
    last_message: Optional[MessageOut] = None