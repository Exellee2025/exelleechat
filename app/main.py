from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, desc, or_
from sqlalchemy.orm import Session, joinedload

from app import models
from app.auth import create_access_token, decode_access_token, hash_password, verify_password
from app.database import Base, SessionLocal, engine, get_db
from app.schemas import (
    ChatCreate,
    ChatJoinRequest,
    ChatOut,
    DirectChatCreate,
    MessageCreate,
    MessageOut,
    ProfileUpdate,
    Token,
    UserCreate,
    UserLogin,
    UserOut,
)

Base.metadata.create_all(bind=engine)

app = FastAPI()
security = HTTPBearer(auto_error=False)

STATIC_DIR = Path("app/static")
AVATAR_DIR = STATIC_DIR / "avatars"
MEDIA_DIR = STATIC_DIR / "media"

STATIC_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

ALLOWED_AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".webm", ".mov",
    ".mp3", ".wav", ".ogg",
    ".pdf", ".txt", ".zip", ".rar",
}
MAX_AVATAR_SIZE = 512 * 1024
MAX_MEDIA_SIZE = 20 * 1024 * 1024
MEDIA_TTL_SECONDS = 3600
MEDIA_CLEANUP_INTERVAL_SECONDS = 300


def safe_trim(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def file_extension(filename: Optional[str]) -> str:
    return Path(filename or "").suffix.lower()


def save_bytes_to_file(directory: Path, content: bytes, ext: str) -> str:
    filename = f"{uuid.uuid4().hex}{ext}"
    path = directory / filename
    with open(path, "wb") as f:
        f.write(content)
    return filename


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    payload = decode_access_token(credentials.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def is_chat_member(db: Session, chat_id: int, user_id: int) -> bool:
    membership = (
        db.query(models.ChatMember)
        .filter(
            models.ChatMember.chat_id == chat_id,
            models.ChatMember.user_id == user_id,
        )
        .first()
    )
    return membership is not None


def ensure_member(db: Session, chat_id: int, user_id: int):
    if not is_chat_member(db, chat_id, user_id):
        db.add(models.ChatMember(chat_id=chat_id, user_id=user_id))
        db.commit()


def serialize_user(user: models.User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "avatar_url": user.avatar_url,
        "avatar_path": user.avatar_path,
        "nickname_color": user.nickname_color,
        "created_at": user.created_at,
    }


def serialize_message(message: models.Message) -> dict:
    return {
        "id": message.id,
        "chat_id": message.chat_id,
        "content": message.content,
        "media_url": message.media_url,
        "media_type": message.media_type,
        "created_at": message.created_at,
        "user": serialize_user(message.user),
    }


def display_name_for_user(user: models.User) -> str:
    full_name = " ".join(
        part for part in [safe_trim(user.first_name), safe_trim(user.last_name)] if part
    ).strip()
    return full_name or user.username


def build_chat_title(chat: models.Chat, current_user_id: int) -> str:
    if not chat.is_direct:
        return chat.title or f"Чат #{chat.id}"

    others = [member.user for member in chat.members if member.user_id != current_user_id]
    if others:
        return display_name_for_user(others[0])

    return chat.title or f"Личка #{chat.id}"


def can_access_chat(db: Session, user_id: int, chat: models.Chat) -> bool:
    if chat.is_direct:
        return is_chat_member(db, chat.id, user_id)

    if chat.password_hash:
        return is_chat_member(db, chat.id, user_id)

    return True


def serialize_chat(chat: models.Chat, current_user_id: int, db: Session) -> dict:
    last_message = None
    if chat.messages:
      ordered = sorted(chat.messages, key=lambda x: (x.created_at, x.id), reverse=True)
      if ordered:
          last_message = serialize_message(ordered[0])

    joined = is_chat_member(db, chat.id, current_user_id)

    if chat.is_public and not chat.password_hash and not chat.is_direct:
        joined = True

    return {
        "id": chat.id,
        "title": build_chat_title(chat, current_user_id),
        "is_direct": chat.is_direct,
        "is_public": chat.is_public,
        "requires_password": bool(chat.password_hash),
        "joined": joined,
        "created_at": chat.created_at,
        "members": [serialize_user(member.user) for member in chat.members],
        "last_message": last_message,
    }


async def cleanup_media_loop():
    while True:
        try:
            now = int(time.time())
            if MEDIA_DIR.exists():
                for file_path in MEDIA_DIR.iterdir():
                    try:
                        if not file_path.is_file():
                            continue
                        age = now - int(file_path.stat().st_mtime)
                        if age > MEDIA_TTL_SECONDS:
                            file_path.unlink(missing_ok=True)
                    except Exception as e:
                        print(f"Media cleanup warning for {file_path.name}: {e}")
        except Exception as e:
            print(f"Media cleanup loop error: {e}")

        await asyncio.sleep(MEDIA_CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_media_loop())


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, list[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(user_id, []).append(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id not in self.active_connections:
            return

        self.active_connections[user_id] = [
            ws for ws in self.active_connections[user_id] if ws is not websocket
        ]
        if not self.active_connections[user_id]:
            del self.active_connections[user_id]

    async def send_to_user(self, user_id: int, payload: dict):
        sockets = list(self.active_connections.get(user_id, []))
        dead = []

        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(user_id, ws)

    async def broadcast_chat(self, chat: models.Chat, payload: dict):
        if chat.is_public and not chat.is_direct and not chat.password_hash:
            targets = list(self.active_connections.keys())
        else:
            targets = list({member.user_id for member in chat.members})

        for user_id in targets:
            await self.send_to_user(user_id, payload)


manager = ConnectionManager()


@app.post("/register", response_model=UserOut)
def register(data: UserCreate, db: Session = Depends(get_db)):
    username = data.username.strip()

    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")

    existing = db.query(models.User).filter(models.User.username == username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    user = models.User(
        username=username,
        password_hash=hash_password(data.password),
        first_name=safe_trim(data.first_name),
        last_name=safe_trim(data.last_name),
        nickname_color=safe_trim(data.nickname_color) or "#4f8cff",
        avatar_url=None,
        avatar_path=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/login", response_model=Token)
def login(data: UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == data.username.strip()).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_access_token({"sub": str(user.id)})
    return Token(access_token=token)


@app.get("/me", response_model=UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


@app.put("/me/profile", response_model=UserOut)
def update_profile(
    payload: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if payload.first_name is not None:
        current_user.first_name = safe_trim(payload.first_name)

    if payload.last_name is not None:
        current_user.last_name = safe_trim(payload.last_name)

    if payload.nickname_color is not None:
        current_user.nickname_color = safe_trim(payload.nickname_color) or "#4f8cff"

    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


@app.post("/me/avatar", response_model=UserOut)
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    ext = file_extension(file.filename)
    if ext not in ALLOWED_AVATAR_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported avatar format")

    content = await file.read()
    if len(content) > MAX_AVATAR_SIZE:
        raise HTTPException(status_code=400, detail="Avatar must be <= 512 KB")

    filename = save_bytes_to_file(AVATAR_DIR, content, ext)
    avatar_url = f"/static/avatars/{filename}"

    current_user.avatar_url = avatar_url
    current_user.avatar_path = avatar_url

    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


@app.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    users = (
        db.query(models.User)
        .filter(models.User.id != current_user.id)
        .order_by(models.User.username.asc())
        .all()
    )
    return users


@app.get("/chats", response_model=list[ChatOut])
def list_chats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    membership_chat_ids = (
        db.query(models.ChatMember.chat_id)
        .filter(models.ChatMember.user_id == current_user.id)
        .subquery()
    )

    chats = (
        db.query(models.Chat)
        .options(
            joinedload(models.Chat.members).joinedload(models.ChatMember.user),
            joinedload(models.Chat.messages).joinedload(models.Message.user),
        )
        .filter(
            or_(
                and_(models.Chat.is_public == True, models.Chat.is_direct == False),
                models.Chat.id.in_(membership_chat_ids),
            )
        )
        .order_by(desc(models.Chat.id))
        .all()
    )

    return [serialize_chat(chat, current_user.id, db) for chat in chats]


@app.post("/chats", response_model=ChatOut)
def create_chat(
    data: ChatCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    title = (data.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Chat title is required")

    if data.is_public and data.create_password != "315146":
        raise HTTPException(status_code=403, detail="Wrong password for public chat creation")

    chat_password = safe_trim(data.chat_password)

    chat = models.Chat(
        title=title,
        is_direct=False,
        is_public=bool(data.is_public),
        created_by_id=current_user.id,
        password_hash=hash_password(chat_password) if chat_password else None,
    )
    db.add(chat)
    db.commit()
    db.refresh(chat)

    db.add(models.ChatMember(chat_id=chat.id, user_id=current_user.id))
    db.commit()

    chat = (
        db.query(models.Chat)
        .options(
            joinedload(models.Chat.members).joinedload(models.ChatMember.user),
            joinedload(models.Chat.messages).joinedload(models.Message.user),
        )
        .filter(models.Chat.id == chat.id)
        .first()
    )

    return serialize_chat(chat, current_user.id, db)


@app.post("/chats/{chat_id}/join")
def join_chat(
    chat_id: int,
    data: ChatJoinRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    chat = (
        db.query(models.Chat)
        .options(joinedload(models.Chat.members).joinedload(models.ChatMember.user))
        .filter(models.Chat.id == chat_id)
        .first()
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if chat.is_direct:
        raise HTTPException(status_code=400, detail="Direct chat cannot be joined this way")

    if is_chat_member(db, chat.id, current_user.id):
        return {"ok": True, "joined": True}

    if chat.password_hash:
        password = (data.password or "").strip()
        if not password or not verify_password(password, chat.password_hash):
            raise HTTPException(status_code=403, detail="Wrong chat password")

    ensure_member(db, chat.id, current_user.id)
    return {"ok": True, "joined": True}


@app.post("/direct-chats", response_model=ChatOut)
def create_direct_chat(
    data: DirectChatCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if data.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot create direct chat with yourself")

    other_user = db.query(models.User).filter(models.User.id == data.user_id).first()
    if not other_user:
        raise HTTPException(status_code=404, detail="User not found")

    direct_chats = (
        db.query(models.Chat)
        .options(joinedload(models.Chat.members).joinedload(models.ChatMember.user))
        .filter(models.Chat.is_direct == True)
        .all()
    )

    for chat in direct_chats:
        member_ids = sorted(member.user_id for member in chat.members)
        if member_ids == sorted([current_user.id, data.user_id]):
            return serialize_chat(chat, current_user.id, db)

    chat = models.Chat(
        title=None,
        is_direct=True,
        is_public=False,
        created_by_id=current_user.id,
        password_hash=None,
    )
    db.add(chat)
    db.commit()
    db.refresh(chat)

    db.add_all(
        [
            models.ChatMember(chat_id=chat.id, user_id=current_user.id),
            models.ChatMember(chat_id=chat.id, user_id=data.user_id),
        ]
    )
    db.commit()

    chat = (
        db.query(models.Chat)
        .options(
            joinedload(models.Chat.members).joinedload(models.ChatMember.user),
            joinedload(models.Chat.messages).joinedload(models.Message.user),
        )
        .filter(models.Chat.id == chat.id)
        .first()
    )

    return serialize_chat(chat, current_user.id, db)


@app.get("/chats/{chat_id}/messages", response_model=list[MessageOut])
def get_messages(
    chat_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(db, current_user.id, chat):
        raise HTTPException(status_code=403, detail="Access denied")

    if chat.is_public and not chat.is_direct and not chat.password_hash:
        ensure_member(db, chat.id, current_user.id)

    messages = (
        db.query(models.Message)
        .options(joinedload(models.Message.user))
        .filter(models.Message.chat_id == chat_id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )

    return [serialize_message(msg) for msg in messages]


@app.post("/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    ext = file_extension(file.filename)
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported media format")

    content = await file.read()
    if len(content) > MAX_MEDIA_SIZE:
        raise HTTPException(status_code=400, detail="Media must be <= 20 MB")

    filename = save_bytes_to_file(MEDIA_DIR, content, ext)
    media_url = f"/static/media/{filename}"

    return {
        "media_url": media_url,
        "media_type": file.content_type or "application/octet-stream",
        "original_name": file.filename,
    }


@app.post("/chats/{chat_id}/messages", response_model=MessageOut)
async def create_message(
    chat_id: int,
    data: MessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    chat = (
        db.query(models.Chat)
        .options(joinedload(models.Chat.members).joinedload(models.ChatMember.user))
        .filter(models.Chat.id == chat_id)
        .first()
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(db, current_user.id, chat):
        raise HTTPException(status_code=403, detail="Access denied")

    if chat.is_public and not chat.is_direct and not chat.password_hash:
        ensure_member(db, chat.id, current_user.id)
        chat = (
            db.query(models.Chat)
            .options(joinedload(models.Chat.members).joinedload(models.ChatMember.user))
            .filter(models.Chat.id == chat_id)
            .first()
        )

    content = (data.content or "").strip()
    media_url = safe_trim(data.media_url)
    media_type = safe_trim(data.media_type)

    if not content and not media_url:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    message = models.Message(
        chat_id=chat_id,
        user_id=current_user.id,
        content=content or "",
        media_url=media_url,
        media_type=media_type,
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    message = (
        db.query(models.Message)
        .options(joinedload(models.Message.user))
        .filter(models.Message.id == message.id)
        .first()
    )

    payload = {
        "type": "message",
        "chat_id": chat_id,
        "message": serialize_message(message),
    }

    try:
        await manager.broadcast_chat(chat, payload)
    except Exception as e:
        print(f"Broadcast warning: {e}")

    return serialize_message(message)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        await websocket.close(code=1008)
        return

    user_id = int(payload["sub"])
    await manager.connect(user_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "typing":
                chat_id_raw = data.get("chat_id")
                if chat_id_raw is None:
                    continue

                chat_id = int(chat_id_raw)
                is_typing = bool(data.get("is_typing", False))

                db = SessionLocal()
                try:
                    chat = (
                        db.query(models.Chat)
                        .options(joinedload(models.Chat.members).joinedload(models.ChatMember.user))
                        .filter(models.Chat.id == chat_id)
                        .first()
                    )
                    if not chat:
                        continue

                    if not can_access_chat(db, user_id, chat):
                        continue

                    user = db.query(models.User).filter(models.User.id == user_id).first()
                    if not user:
                        continue

                    await manager.broadcast_chat(
                        chat,
                        {
                            "type": "typing",
                            "chat_id": chat_id,
                            "is_typing": is_typing,
                            "user": serialize_user(user),
                        },
                    )
                finally:
                    db.close()
            else:
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
    except Exception:
        manager.disconnect(user_id, websocket)


@app.get("/")
def root():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"status": "ok"}


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    if full_path.startswith("ws"):
        raise HTTPException(status_code=404, detail="Not found")

    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)

    raise HTTPException(status_code=404, detail="Frontend not found")