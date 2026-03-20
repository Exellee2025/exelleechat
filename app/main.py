from datetime import datetime
from typing import Dict, List

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app import models
from app.auth import create_access_token, decode_access_token, hash_password, verify_password
from app.database import Base, engine, get_db, SessionLocal
from app.schemas import (
    Token,
    UserCreate,
    UserLogin,
    UserOut,
    ChatCreate,
    ChatOut,
    MessageCreate,
    MessageOut,
    DirectChatCreate,
)

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# WebSocket connection manager
# -----------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, chat_id: int, websocket: WebSocket):
        await websocket.accept()
        if chat_id not in self.active_connections:
            self.active_connections[chat_id] = []
        self.active_connections[chat_id].append(websocket)

    def disconnect(self, chat_id: int, websocket: WebSocket):
        if chat_id in self.active_connections:
            if websocket in self.active_connections[chat_id]:
                self.active_connections[chat_id].remove(websocket)
            if not self.active_connections[chat_id]:
                del self.active_connections[chat_id]

    async def broadcast(self, chat_id: int, message: str):
        if chat_id not in self.active_connections:
            return

        dead_connections = []

        for connection in self.active_connections[chat_id]:
            try:
                await connection.send_text(message)
            except Exception:
                dead_connections.append(connection)

        for connection in dead_connections:
            self.disconnect(chat_id, connection)


manager = ConnectionManager()


# -----------------------------
# Helpers
# -----------------------------
def serialize_user(user):
    return {
        "id": user.id,
        "username": getattr(user, "username", None),
    }


def serialize_chat(chat):
    return {
        "id": chat.id,
        "title": getattr(chat, "title", None),
        "is_public": bool(getattr(chat, "is_public", False)),
    }


def serialize_message(message):
    sender = getattr(message, "user", None)
    return {
        "id": message.id,
        "chat_id": message.chat_id,
        "user_id": message.user_id,
        "content": message.content,
        "created_at": message.created_at.isoformat() if getattr(message, "created_at", None) else None,
        "username": getattr(sender, "username", None) if sender else None,
    }


def get_user_from_token(token: str, db: Session):
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            return None
        user = db.query(models.User).filter(models.User.id == int(user_id)).first()
        return user
    except Exception:
        return None


def get_current_user(token: str = Query(...), db: Session = Depends(get_db)):
    user = get_user_from_token(token, db)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def user_in_chat(chat, user_id: int) -> bool:
    users = getattr(chat, "users", None)
    if not users:
        return False
    member_ids = [u.id for u in users]
    return user_id in member_ids


def can_access_chat(chat, user_id: int) -> bool:
    if not chat:
        return False
    if bool(getattr(chat, "is_public", False)):
        return True
    return user_in_chat(chat, user_id)


# -----------------------------
# Auth
# -----------------------------
@app.post("/register", response_model=UserOut)
def register(user: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.username == user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    new_user = models.User(
        username=user.username,
        password_hash=hash_password(user.password),
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
def me(current_user=Depends(get_current_user)):
    return current_user


# -----------------------------
# Chats
# -----------------------------
@app.get("/chats")
def get_chats(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    all_chats = db.query(models.Chat).all()

    visible_chats = []
    for chat in all_chats:
        if can_access_chat(chat, current_user.id):
            visible_chats.append(serialize_chat(chat))

    return visible_chats


@app.post("/chats")
def create_chat(chat: ChatCreate, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    title = getattr(chat, "title", None)
    is_public = bool(getattr(chat, "is_public", False))

    new_chat = models.Chat(
        title=title,
        is_public=is_public,
        created_by_id=current_user.id,
    )

    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)

    # если чат не публичный, добавим создателя в участники
    if not is_public and hasattr(new_chat, "users"):
        new_chat.users.append(current_user)
        db.commit()
        db.refresh(new_chat)

    return serialize_chat(new_chat)


@app.post("/direct-chats")
def create_direct_chat(
    data: DirectChatCreate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    other_user_id = getattr(data, "user_id", None)
    if other_user_id is None:
        raise HTTPException(status_code=400, detail="user_id is required")

    if other_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot create direct chat with yourself")

    other_user = db.query(models.User).filter(models.User.id == other_user_id).first()
    if not other_user:
        raise HTTPException(status_code=404, detail="User not found")

    chats = db.query(models.Chat).filter(models.Chat.is_public == False).all()

    for chat in chats:
        users = getattr(chat, "users", [])
        user_ids = sorted([u.id for u in users]) if users else []
        if user_ids == sorted([current_user.id, other_user_id]):
            return serialize_chat(chat)

    new_chat = models.Chat(
        title=f"{current_user.username} & {other_user.username}",
        is_public=False,
        created_by_id=current_user.id,
    )
    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)

    if hasattr(new_chat, "users"):
        new_chat.users.append(current_user)
        new_chat.users.append(other_user)
        db.commit()
        db.refresh(new_chat)

    return serialize_chat(new_chat)


@app.get("/chats/{chat_id}")
def get_chat(chat_id: int, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(chat, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    return serialize_chat(chat)


# -----------------------------
# Messages
# -----------------------------
@app.get("/chats/{chat_id}/messages")
def get_messages(chat_id: int, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(chat, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    messages = (
        db.query(models.Message)
        .filter(models.Message.chat_id == chat_id)
        .order_by(models.Message.created_at.asc())
        .all()
    )

    return [serialize_message(m) for m in messages]


@app.post("/chats/{chat_id}/messages")
async def create_message(
    chat_id: int,
    message: MessageCreate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not can_access_chat(chat, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    content = getattr(message, "content", "").strip()
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

    # Подтянем автора для сериализации
    new_message.user = current_user

    payload = serialize_message(new_message)

    try:
        import json
        await manager.broadcast(chat_id, json.dumps({
            "type": "new_message",
            "message": payload
        }, ensure_ascii=False))
    except Exception:
        pass

    return payload


# -----------------------------
# WebSocket
# -----------------------------
@app.websocket("/ws/{chat_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: int, token: str = Query(...)):
    db = SessionLocal()
    user = None

    try:
        # 1. Проверяем токен
        user = get_user_from_token(token, db)
        if not user:
            await websocket.close(code=1008)
            return

        # 2. Проверяем чат
        chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
        if not chat:
            await websocket.close(code=1008)
            return

        # 3. Проверяем доступ
        if not can_access_chat(chat, user.id):
            await websocket.close(code=1008)
            return

        # 4. Подключаем
        await manager.connect(chat_id, websocket)

        # 5. Слушаем сокет
        while True:
            data = await websocket.receive_text()

            # Можно отправлять ping с фронта, чтобы держать соединение живым
            if data == "ping":
                await websocket.send_text("pong")
                continue

            # Можно отправлять typing-события строкой
            if data == "typing":
                try:
                    import json
                    await manager.broadcast(chat_id, json.dumps({
                        "type": "typing",
                        "user_id": user.id,
                        "username": user.username,
                    }, ensure_ascii=False))
                except Exception:
                    pass
                continue

            # Остальное просто не валим сервером
            try:
                import json
                await websocket.send_text(json.dumps({
                    "type": "info",
                    "message": "unknown websocket event"
                }, ensure_ascii=False))
            except Exception:
                pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("WebSocket error:", repr(e))
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        try:
            manager.disconnect(chat_id, websocket)
        except Exception:
            pass
        db.close()