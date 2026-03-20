from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, desc, or_
from sqlalchemy.orm import Session, joinedload

from app import models
from app.auth import create_access_token, decode_access_token, hash_password, verify_password
from app.database import Base, engine, get_db, SessionLocal
from app.schemas import (
    AvatarUpdate,
    ChatCreate,
    ChatOut,
    DirectChatCreate,
    MessageCreate,
    MessageOut,
    Token,
    UserCreate,
    UserLogin,
    UserOut,
)

Base.metadata.create_all(bind=engine)

app = FastAPI()
security = HTTPBearer(auto_error=False)

STATIC_DIR = Path("app/static")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


def serialize_user(user: models.User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at,
    }


def serialize_message(message: models.Message) -> dict:
    return {
        "id": message.id,
        "chat_id": message.chat_id,
        "content": message.content,
        "created_at": message.created_at,
        "user": serialize_user(message.user),
    }


def build_chat_title(chat: models.Chat, current_user_id: int) -> str:
    if not chat.is_direct:
        return chat.title or f"Чат #{chat.id}"

    others = [member.user.username for member in chat.members if member.user_id != current_user_id]
    return others[0] if others else (chat.title or f"Личка #{chat.id}")


def can_access_chat(db: Session, user_id: int, chat: models.Chat) -> bool:
    if chat.is_public and not chat.is_direct:
        return True

    membership = (
        db.query(models.ChatMember)
        .filter(
            models.ChatMember.chat_id == chat.id,
            models.ChatMember.user_id == user_id,
        )
        .first()
    )
    return membership is not None


def ensure_member(db: Session, chat_id: int, user_id: int):
    membership = (
        db.query(models.ChatMember)
        .filter(
            models.ChatMember.chat_id == chat_id,
            models.ChatMember.user_id == user_id,
        )
        .first()
    )
    if not membership:
        db.add(models.ChatMember(chat_id=chat_id, user_id=user_id))
        db.commit()


def serialize_chat(chat: models.Chat, current_user_id: int) -> dict:
    last_message = None
    if chat.messages:
        ordered = sorted(chat.messages, key=lambda x: x.created_at, reverse=True)
        if ordered:
            last_message = serialize_message(ordered[0])

    return {
        "id": chat.id,
        "title": build_chat_title(chat, current_user_id),
        "is_direct": chat.is_direct,
        "is_public": chat.is_public,
        "created_at": chat.created_at,
        "members": [serialize_user(member.user) for member in chat.members],
        "last_message": last_message,
    }


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, list[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(user_id, []).append(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            self.active_connections[user_id] = [
                ws for ws in self.active_connections[user_id] if ws != websocket
            ]
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_to_user(self, user_id: int, payload: dict):
        for ws in self.active_connections.get(user_id, []):
            await ws.send_json(payload)

    async def broadcast_chat(self, chat: models.Chat, payload: dict):
        if chat.is_public and not chat.is_direct:
            targets = set(self.active_connections.keys())
        else:
            targets = {member.user_id for member in chat.members}

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
        avatar_url=(data.avatar_url or "").strip() or None,
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


@app.put("/me/avatar", response_model=UserOut)
def update_avatar(
    payload: AvatarUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    current_user.avatar_url = (payload.avatar_url or "").strip() or None
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

    return [serialize_chat(chat, current_user.id) for chat in chats]


@app.post("/chats", response_model=ChatOut)
def create_chat(
    data: ChatCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Chat title is required")

    if data.is_public:
        if data.create_password != "315146":
            raise HTTPException(status_code=403, detail="Wrong password for public chat creation")

    chat = models.Chat(
        title=title,
        is_direct=False,
        is_public=data.is_public,
        created_by_id=current_user.id,
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

    return serialize_chat(chat, current_user.id)


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
            return serialize_chat(chat, current_user.id)

    chat = models.Chat(
        title=None,
        is_direct=True,
        is_public=False,
        created_by_id=current_user.id,
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

    return serialize_chat(chat, current_user.id)


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

    if chat.is_public and not chat.is_direct:
        ensure_member(db, chat.id, current_user.id)

    messages = (
        db.query(models.Message)
        .options(joinedload(models.Message.user))
        .filter(models.Message.chat_id == chat_id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )

    return [serialize_message(msg) for msg in messages]


@app.post("/chats/{chat_id}/messages", response_model=MessageOut)
async def create_message(
    chat_id: int,
    data: MessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    chat = (
        db.query(models.Chat)
        .options(
            joinedload(models.Chat.members).joinedload(models.ChatMember.user),
            joinedload(models.Chat.messages).joinedload(models.Message.user),
        )
        .filter(models.Chat.id == chat_id)
        .first()
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(db, current_user.id, chat):
        raise HTTPException(status_code=403, detail="Access denied")

    if chat.is_public and not chat.is_direct:
        ensure_member(db, chat.id, current_user.id)
        chat = (
            db.query(models.Chat)
            .options(joinedload(models.Chat.members).joinedload(models.ChatMember.user))
            .filter(models.Chat.id == chat_id)
            .first()
        )

    content = data.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    message = models.Message(chat_id=chat_id, user_id=current_user.id, content=content)
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
    await manager.broadcast_chat(chat, payload)

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