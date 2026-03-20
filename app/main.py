import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Set

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload

from app import models
from app.auth import create_access_token, decode_access_token, hash_password, verify_password
from app.database import Base, SessionLocal, engine, get_db
from app.schemas import (
    ChatCreate,
    DirectChatCreate,
    MessageCreate,
    Token,
    UserCreate,
    UserLogin,
    UserOut,
)

Base.metadata.create_all(bind=engine)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(APP_DIR)
FLUTTER_BUILD_DIR = os.path.join(PROJECT_ROOT, "build", "web")

app = FastAPI(title="Exellee Chat")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_CHAT_CREATION_PASSWORD = "315146"


def pick_avatar_color(username: str) -> str:
    palette = [
        "#4f46e5",
        "#0ea5e9",
        "#16a34a",
        "#f97316",
        "#e11d48",
        "#a855f7",
        "#14b8a6",
        "#f59e0b",
    ]
    idx = sum(ord(c) for c in username) % len(palette)
    return palette[idx]


def serialize_user(user: models.User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "avatar_color": user.avatar_color,
    }


def direct_chat_display_title(chat: models.Chat, current_user_id: int) -> str:
    if chat.is_public:
        return chat.title

    users = chat.users or []
    if len(users) == 2:
        other = next((u for u in users if u.id != current_user_id), None)
        if other:
            return other.username
    return chat.title


def serialize_chat(chat: models.Chat, current_user_id: Optional[int] = None) -> dict:
    title = chat.title
    if current_user_id is not None:
        title = direct_chat_display_title(chat, current_user_id)

    return {
        "id": chat.id,
        "title": title,
        "is_public": chat.is_public,
    }


def serialize_message(message: models.Message) -> dict:
    return {
        "id": message.id,
        "chat_id": message.chat_id,
        "user_id": message.user_id,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "username": message.user.username if message.user else None,
        "avatar_color": message.user.avatar_color if message.user else None,
    }


def get_user_from_token(token: str, db: Session) -> Optional[models.User]:
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            return None
        return db.query(models.User).filter(models.User.id == int(user_id)).first()
    except Exception:
        return None


def get_current_user(token: str = Query(...), db: Session = Depends(get_db)) -> models.User:
    user = get_user_from_token(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def user_in_chat(chat: models.Chat, user_id: int) -> bool:
    users = getattr(chat, "users", None) or []
    return any(u.id == user_id for u in users)


def can_access_chat(chat: models.Chat, user_id: int) -> bool:
    if chat.is_public:
        return True
    return user_in_chat(chat, user_id)


class ConnectionManager:
    def __init__(self):
        self.chat_connections: Dict[int, List[WebSocket]] = defaultdict(list)
        self.online_users: Set[int] = set()

    async def connect(self, chat_id: int, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.chat_connections[chat_id].append(websocket)
        self.online_users.add(user_id)

    def disconnect(self, chat_id: int, user_id: int, websocket: WebSocket):
        if chat_id in self.chat_connections and websocket in self.chat_connections[chat_id]:
            self.chat_connections[chat_id].remove(websocket)
            if not self.chat_connections[chat_id]:
                del self.chat_connections[chat_id]
        self.online_users.discard(user_id)

    async def broadcast(self, chat_id: int, payload: dict):
        if chat_id not in self.chat_connections:
            return

        dead = []
        message = json.dumps(payload, ensure_ascii=False)
        for ws in self.chat_connections[chat_id]:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            if ws in self.chat_connections.get(chat_id, []):
                self.chat_connections[chat_id].remove(ws)

    def online_list(self, db: Session) -> list:
        if not self.online_users:
            return []
        users = db.query(models.User).filter(models.User.id.in_(self.online_users)).all()
        return [serialize_user(u) for u in users]


manager = ConnectionManager()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/register", response_model=UserOut)
def register(user: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.username == user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    new_user = models.User(
        username=user.username,
        password_hash=hash_password(user.password),
        avatar_color=pick_avatar_color(user.username),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.post("/login", response_model=Token)
def login(user: UserLogin, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if not db_user or not verify_password(user.password, db_user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    access_token = create_access_token({"sub": str(db_user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/me", response_model=UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


@app.get("/users")
def get_users(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    users = (
        db.query(models.User)
        .filter(models.User.id != current_user.id)
        .order_by(models.User.username.asc())
        .all()
    )
    return [serialize_user(u) for u in users]


@app.get("/online-users")
def get_online_users(db: Session = Depends(get_db)):
    return manager.online_list(db)


@app.get("/chats")
def get_chats(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    chats = db.query(models.Chat).options(joinedload(models.Chat.users)).order_by(models.Chat.created_at.desc()).all()
    visible = [serialize_chat(chat, current_user.id) for chat in chats if can_access_chat(chat, current_user.id)]
    return visible


@app.post("/chats")
def create_chat(
    chat: ChatCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    title = chat.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Chat title cannot be empty")

    if chat.is_public and chat.creation_password != PUBLIC_CHAT_CREATION_PASSWORD:
        raise HTTPException(status_code=403, detail="Wrong creation password")

    new_chat = models.Chat(
        title=title,
        is_public=chat.is_public,
        created_by_id=current_user.id,
    )
    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)

    if not chat.is_public:
        new_chat.users.append(current_user)
        db.commit()
        db.refresh(new_chat)

    return serialize_chat(new_chat, current_user.id)


@app.post("/direct-chats")
def create_direct_chat(
    data: DirectChatCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot create direct chat with yourself")

    other_user = db.query(models.User).filter(models.User.id == data.user_id).first()
    if not other_user:
        raise HTTPException(status_code=404, detail="User not found")

    private_chats = (
        db.query(models.Chat)
        .filter(models.Chat.is_public == False)  # noqa: E712
        .options(joinedload(models.Chat.users))
        .all()
    )

    for chat in private_chats:
        ids = sorted([u.id for u in chat.users])
        if ids == sorted([current_user.id, other_user.id]):
            return serialize_chat(chat, current_user.id)

    new_chat = models.Chat(
        title=f"{current_user.username} & {other_user.username}",
        is_public=False,
        created_by_id=current_user.id,
    )
    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)

    new_chat.users.append(current_user)
    new_chat.users.append(other_user)
    db.commit()
    db.refresh(new_chat)

    return serialize_chat(new_chat, current_user.id)


@app.get("/chats/{chat_id}/messages")
def get_messages(
    chat_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    chat = (
        db.query(models.Chat)
        .options(joinedload(models.Chat.users))
        .filter(models.Chat.id == chat_id)
        .first()
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(chat, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    messages = (
        db.query(models.Message)
        .options(joinedload(models.Message.user))
        .filter(models.Message.chat_id == chat_id)
        .order_by(models.Message.created_at.asc())
        .all()
    )
    return [serialize_message(m) for m in messages]


@app.post("/chats/{chat_id}/messages")
async def create_message(
    chat_id: int,
    message: MessageCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    chat = (
        db.query(models.Chat)
        .options(joinedload(models.Chat.users))
        .filter(models.Chat.id == chat_id)
        .first()
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(chat, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    content = message.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    new_message = models.Message(
        chat_id=chat_id,
        user_id=current_user.id,
        content=content,
        created_at=datetime.utcnow(),
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)
    db.refresh(current_user)

    new_message.user = current_user

    payload = serialize_message(new_message)
    await manager.broadcast(chat_id, {"type": "new_message", "message": payload})
    return payload


@app.websocket("/ws/{chat_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: int, token: str = Query(...)):
    db = SessionLocal()
    user = None

    try:
        user = get_user_from_token(token, db)
        if not user:
            await websocket.close(code=1008)
            return

        chat = (
            db.query(models.Chat)
            .options(joinedload(models.Chat.users))
            .filter(models.Chat.id == chat_id)
            .first()
        )
        if not chat or not can_access_chat(chat, user.id):
            await websocket.close(code=1008)
            return

        await manager.connect(chat_id, user.id, websocket)
        await manager.broadcast(chat_id, {"type": "online_users", "users": manager.online_list(db)})

        while True:
            data = await websocket.receive_text()

            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            if data == "typing":
                await manager.broadcast(
                    chat_id,
                    {
                        "type": "typing",
                        "chat_id": chat_id,
                        "user_id": user.id,
                        "username": user.username,
                    },
                )
                continue

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("WebSocket error:", repr(e))
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if user:
            manager.disconnect(chat_id, user.id, websocket)
            try:
                await manager.broadcast(chat_id, {"type": "online_users", "users": manager.online_list(db)})
            except Exception:
                pass
        db.close()


if os.path.isdir(FLUTTER_BUILD_DIR):
    assets_dir = os.path.join(FLUTTER_BUILD_DIR, "assets")
    canvaskit_dir = os.path.join(FLUTTER_BUILD_DIR, "canvaskit")
    icons_dir = os.path.join(FLUTTER_BUILD_DIR, "icons")

    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    if os.path.isdir(canvaskit_dir):
        app.mount("/canvaskit", StaticFiles(directory=canvaskit_dir), name="canvaskit")
    if os.path.isdir(icons_dir):
        app.mount("/icons", StaticFiles(directory=icons_dir), name="icons")


@app.get("/")
def serve_root():
    index_file = os.path.join(FLUTTER_BUILD_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {
        "status": "error",
        "message": "Flutter web build not found. Run: flutter build web"
    }


@app.get("/{full_path:path}")
def serve_flutter(full_path: str):
    if full_path.startswith("docs") or full_path.startswith("openapi.json"):
        raise HTTPException(status_code=404, detail="Not Found")

    file_path = os.path.join(FLUTTER_BUILD_DIR, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)

    index_file = os.path.join(FLUTTER_BUILD_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)

    raise HTTPException(status_code=404, detail="Flutter web build not found")