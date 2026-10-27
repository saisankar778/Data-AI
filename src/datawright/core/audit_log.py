from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from datawright.models import AuditLog


def record(
    session: Session, actor: str, action: str, object_type: str, object_id: object, **detail: Any
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            object_type=object_type,
            object_id=str(object_id),
            detail=detail,
        )
    )
