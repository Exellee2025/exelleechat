from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str
    password: str
    avatar_url: Optional[str] = None


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    avatar_url: Optional[str] = None
    created_at: datetime


class AvatarUpdate(BaseModel):
    avatar_url: Optional[str] = None


class ChatCreate(BaseModel):
    title: str
    is_public: bool = True
    create_password: Optional[str] = None


class DirectChatCreate(BaseModel):
    user_id: int


class MessageCreate(BaseModel):
    content: str


class MessageOut(BaseModel):
    id: int
    chat_id: int
    content: str
    created_at: datetime
    user: UserOut


class ChatOut(BaseModel):
    id: int
    title: str
    is_direct: bool
    is_public: bool
    created_at: datetime
    members: list[UserOut]
    last_message: Optional[MessageOut] = None