from sqlmodel import select

from app.db import get_session
from app.models import User


def is_admin(username: str | None) -> bool:
    if not username:
        return False
    with get_session() as session:
        user = session.exec(select(User).where(User.username == username)).first()
        return bool(user and user.is_admin)
