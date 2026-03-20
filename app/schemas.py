from datetime import datetime
from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: EmailStr
    created_at: datetime

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str


class ChatCreate(BaseModel):
    title: str | None = None
    password: str


class ChatOut(BaseModel):
    id: int
    title: str | None = None
    display_title: str
    is_direct: bool = False
    avatar_text: str | None = None
    subtitle: str | None = None


class MessageCreate(BaseModel):
    text: str


class MessageOut(BaseModel):
    id: int
    text: str
    created_at: datetime
    chat_id: int
    user_id: int

    class Config:
        from_attributes = True


class DirectChatCreate(BaseModel):
    user_id: int


class ChatParticipantOut(BaseModel):
    user_id: int

    class Config:
        from_attributes = True


class OnlineUserOut(BaseModel):
    id: int
    username: str
    email: EmailStr
    avatar_text: str | None = None