from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.db import connect
from app.security import create_token, decode_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str
    password: str


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"username": payload["sub"], "role": payload["role"]}


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


@router.post("/register")
def register(body: Credentials):
    username = body.username.strip()
    if len(username) < 3 or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Username ≥3 chars, password ≥6 chars")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM users")
            is_first = cur.fetchone()["n"] == 0
            cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Username taken")
            role = "admin" if is_first else "viewer"
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
                (username, hash_password(body.password), role),
            )
        conn.commit()
    return {"username": username, "role": role}


@router.post("/login")
def login(body: Credentials):
    username = body.username.strip()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, password_hash, role FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(row["username"], row["role"])
    return {"token": token, "username": row["username"], "role": row["role"]}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return user
