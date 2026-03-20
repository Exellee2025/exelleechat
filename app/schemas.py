from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Token(BaseModel):
    access_token: str
    token_type: str


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=3, max_length=100)


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    avatar_color: str

    class Config:
        from_attributes = True


class ChatCreate(BaseModel):
    title: str
    is_public: bool = False
    creation_password: Optional[str] = None


class DirectChatCreate(BaseModel):
    user_id: int


class ChatOut(BaseModel):
    id: int
    title: str
    is_public: bool


class MessageCreate(BaseModel):
    content: str


class MessageOut(BaseModel):
    id: int
    chat_id: int
    user_id: int
    content: str
    created_at: datetime
    username: Optional[str] = None
    avatar_color: Optional[str] = None