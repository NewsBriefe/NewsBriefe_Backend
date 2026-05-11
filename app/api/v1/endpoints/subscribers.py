"""
Subscriber endpoints

POST /v1/subscribe          — add email
DELETE /v1/subscribe        — delete email (by email in body)
"""
import re
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, field_validator
from typing import Annotated
from app.core.database import get_db
from app.models.subscriber import Subscriber
from email_validator import validate_email, EmailNotValidError

router = APIRouter(tags=["subscribe"])
DBDep = Annotated[AsyncSession, Depends(get_db)]

# Comprehensive email regex pattern
_EMAIL_RE = re.compile(
    r"^(?!\.)[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$"
)

# Common disposable email domains (optional - remove if not needed)
DISPOSABLE_DOMAINS = {
    'tempmail.com', '10minutemail.com', 'mailinator.com', 
    'yopmail.com', 'guerrillamail.com'
}


class SubscribeRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        try:
            # This handles all the complex validation for you
            valid = validate_email(v, check_deliverability=False)
            return valid.email
        except EmailNotValidError as e:
            # Convert to string to ensure JSON serializable
            raise ValueError(str(e))


class SubscribeResponse(BaseModel):
    message: str
    email: str


@router.post(
    "/subscribe",
    response_model=SubscribeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe email for updates",
)
async def subscribe(body: SubscribeRequest, db: DBDep) -> SubscribeResponse:
    existing = (await db.execute(
        select(Subscriber).where(Subscriber.email == body.email)
    )).scalar_one_or_none()

    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.subscribed_at = datetime.now(timezone.utc)
            await db.commit()
            return SubscribeResponse(message="Re-subscribed successfully", email=existing.email)
        return SubscribeResponse(message="Already subscribed", email=existing.email)

    db.add(Subscriber(
        email=body.email,
        is_active=True,
        subscribed_at=datetime.now(timezone.utc),
    ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return SubscribeResponse(message="Already subscribed", email=body.email)

    return SubscribeResponse(message="Subscribed successfully", email=body.email)


@router.delete(
    "/subscribe",
    response_model=SubscribeResponse,
    summary="Unsubscribe and delete email",
)
async def unsubscribe(body: SubscribeRequest, db: DBDep) -> SubscribeResponse:
    # First validate the email format
    try:
        validated_email = SubscribeRequest.validate_email(body.email)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_email", "message": str(e)},
        )
    
    existing = (await db.execute(
        select(Subscriber).where(Subscriber.email == validated_email)
    )).scalar_one_or_none()

    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Email not found"},
        )

    await db.execute(delete(Subscriber).where(Subscriber.email == validated_email))
    await db.commit()

    return SubscribeResponse(message="Email deleted successfully", email=validated_email)