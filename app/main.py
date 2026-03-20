from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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

app = FastAPI()
security = HTTPBearer()

app.mount("/static", StaticFiles(directory="app/static"), name="static")

active_connections: dict[int, list[WebSocket]] = {}
online_users: dict[int, str] = {}


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    token = credentials.credentials
    payload = decode_access_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def get_current_user_from_token(token: str, db: Session):
    payload = decode_access_token(token)

    if not payload:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    return user


@app.get("/")
def read_index():
    return FileResponse("app/static/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/register", response_model=UserOut)
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    existing_email = db.query(models.User).filter(models.User.email == user.email).first()
    if existing_email:
        raise HTTPException(status_code=400, detail="Email already registered")

    existing_username = db.query(models.User).filter(models.User.username == user.username).first()
    if existing_username:
        raise HTTPException(status_code=400, detail="Username already taken")

    new_user = models.User(
        username=user.username,
        email=user.email,
        password=hash_password(user.password),
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.post("/login", response_model=Token)
def login_user(user: UserLogin, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.email == user.email).first()

    if not db_user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(user.password, db_user.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = create_access_token(
        {"sub": str(db_user.id), "email": db_user.email}
    )

    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


@app.get("/logout")
def logout():
    return {"message": "Выход из аккаунта успешен."}


@app.get("/me", response_model=UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


@app.get("/users", response_model=list[UserOut])
def get_users(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return db.query(models.User).all()


@app.get("/online-users")
def get_online_users(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    ids = list(online_users.keys())

    if not ids:
        return []

    users = db.query(models.User).filter(models.User.id.in_(ids)).all()

    return [
        {
            "id": user.id,
            "username": user.username,
            "email": user.email,
        }
        for user in users
    ]


@app.post("/chats", response_model=ChatOut)
def create_chat(
    chat: ChatCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    new_chat = models.Chat(title=chat.title)

    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)

    participant = models.ChatParticipant(chat_id=new_chat.id, user_id=current_user.id)
    db.add(participant)
    db.commit()

    return new_chat


@app.get("/chats", response_model=list[ChatOut])
def get_chats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    participant_links = (
        db.query(models.ChatParticipant)
        .filter(models.ChatParticipant.user_id == current_user.id)
        .all()
    )

    chat_ids = [item.chat_id for item in participant_links]

    if not chat_ids:
        return []

    chats = db.query(models.Chat).filter(models.Chat.id.in_(chat_ids)).all()
    return chats


@app.post("/direct-chats", response_model=ChatOut)
def create_direct_chat(
    data: DirectChatCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if data.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot create chat with yourself")

    other_user = db.query(models.User).filter(models.User.id == data.user_id).first()
    if not other_user:
        raise HTTPException(status_code=404, detail="User not found")

    my_chat_links = (
        db.query(models.ChatParticipant)
        .filter(models.ChatParticipant.user_id == current_user.id)
        .all()
    )

    my_chat_ids = [item.chat_id for item in my_chat_links]

    if my_chat_ids:
        existing_chat = (
            db.query(models.ChatParticipant)
            .filter(
                models.ChatParticipant.chat_id.in_(my_chat_ids),
                models.ChatParticipant.user_id == data.user_id
            )
            .first()
        )

        if existing_chat:
            chat = db.query(models.Chat).filter(models.Chat.id == existing_chat.chat_id).first()
            return chat

    new_chat = models.Chat(title=None)
    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)

    participant_1 = models.ChatParticipant(chat_id=new_chat.id, user_id=current_user.id)
    participant_2 = models.ChatParticipant(chat_id=new_chat.id, user_id=data.user_id)

    db.add(participant_1)
    db.add(participant_2)
    db.commit()

    return new_chat


@app.post("/chats/{chat_id}/messages", response_model=MessageOut)
async def send_message(
    chat_id: int,
    message: MessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    participant = (
        db.query(models.ChatParticipant)
        .filter(
            models.ChatParticipant.chat_id == chat_id,
            models.ChatParticipant.user_id == current_user.id,
        )
        .first()
    )
    if not participant:
        raise HTTPException(status_code=403, detail="You are not a participant of this chat")

    new_message = models.Message(
        text=message.text,
        chat_id=chat_id,
        user_id=current_user.id,
    )

    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    for connection in active_connections.get(chat_id, []):
        await connection.send_text(f"{current_user.username}: {message.text}")

    return new_message


@app.get("/chats/{chat_id}/messages", response_model=list[MessageOut])
def get_messages(
    chat_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    participant = (
        db.query(models.ChatParticipant)
        .filter(
            models.ChatParticipant.chat_id == chat_id,
            models.ChatParticipant.user_id == current_user.id,
        )
        .first()
    )
    if not participant:
        raise HTTPException(status_code=403, detail="You are not a participant of this chat")

    messages = (
        db.query(models.Message)
        .filter(models.Message.chat_id == chat_id)
        .order_by(models.Message.id.asc())
        .all()
    )

    return messages


@app.websocket("/ws/{chat_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    chat_id: int,
    token: str = Query(...)
):
    db = SessionLocal()
    try:
        user = get_current_user_from_token(token, db)
        if not user:
            await websocket.close(code=1008)
            return

        participant = (
            db.query(models.ChatParticipant)
            .filter(
                models.ChatParticipant.chat_id == chat_id,
                models.ChatParticipant.user_id == user.id,
            )
            .first()
        )
        if not participant:
            await websocket.close(code=1008)
            return

        await websocket.accept()

        online_users[user.id] = user.username

        if chat_id not in active_connections:
            active_connections[chat_id] = []
        active_connections[chat_id].append(websocket)

        try:
            while True:
                data = await websocket.receive_text()

                for connection in active_connections.get(chat_id, []):
                    if connection != websocket:
                        await connection.send_text(f"{user.username}: {data}")
        except WebSocketDisconnect:
            if chat_id in active_connections and websocket in active_connections[chat_id]:
                active_connections[chat_id].remove(websocket)

            if chat_id in active_connections and not active_connections[chat_id]:
                del active_connections[chat_id]

            still_online = any(
                user.id in [
                    get_current_user_from_token(token, db).id
                    for token in []
                ]
            )
            # Заглушка не нужна, просто удаляем юзера при закрытии текущего сокета
            if user.id in online_users:
                del online_users[user.id]
    finally:
        db.close()