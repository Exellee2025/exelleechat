from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)

    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)

    avatar_url = Column(String(1000), nullable=True)
    avatar_path = Column(String(1000), nullable=True)

    nickname_color = Column(String(20), nullable=False, default="#4f8cff")

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    memberships = relationship("ChatMember", back_populates="user", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="user")
    created_chats = relationship("Chat", back_populates="created_by")


class Chat(Base):
    __tablename__ = "chats"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=True)

    is_direct = Column(Boolean, default=False, nullable=False)
    is_public = Column(Boolean, default=True, nullable=False)

    password_hash = Column(String(255), nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    created_by = relationship("User", back_populates="created_chats")
    members = relationship("ChatMember", back_populates="chat", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="chat", cascade="all, delete-orphan")


class ChatMember(Base):
    __tablename__ = "chat_members"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_chat_member"),
    )

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, ForeignKey("chats.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    chat = relationship("Chat", back_populates="members")
    user = relationship("User", back_populates="memberships")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, ForeignKey("chats.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    content = Column(Text, nullable=True)

    media_url = Column(String(1000), nullable=True)
    media_type = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    chat = relationship("Chat", back_populates="messages")
    user = relationship("User", back_populates="messages")