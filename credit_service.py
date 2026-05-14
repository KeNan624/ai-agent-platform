from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy.orm import Session

from models import Credit, CreditTransaction, User


class InsufficientCreditsError(RuntimeError):
    def __init__(self, required: Decimal, balance: Decimal):
        self.required = required
        self.balance = balance
        super().__init__(f"积分余额不足，需要 {required}，当前 {balance}")


def as_credit_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value if value is not None else "0").strip() or "0")
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    if amount < 0:
        amount = Decimal("0")
    return amount.quantize(Decimal("0.01"))


def _get_or_create_credit(db: Session, user_id: int, *, for_update: bool = False) -> Credit:
    query = db.query(Credit).filter(Credit.user_id == int(user_id))
    if for_update:
        query = query.with_for_update()
    credit = query.first()
    if credit is not None:
        return credit
    credit = Credit(
        user_id=int(user_id),
        balance=Decimal("0.00"),
        total_bought=Decimal("0.00"),
        total_used=Decimal("0.00"),
    )
    db.add(credit)
    db.flush()
    return credit


def get_credit_summary(user: User, db: Session) -> dict:
    credit = db.query(Credit).filter(Credit.user_id == int(user.id)).first()
    if credit is None:
        return {
            "credit_balance": "0.00",
            "credit_total_bought": "0.00",
            "credit_total_used": "0.00",
        }
    return {
        "credit_balance": f"{as_credit_amount(credit.balance)}",
        "credit_total_bought": f"{as_credit_amount(credit.total_bought)}",
        "credit_total_used": f"{as_credit_amount(credit.total_used)}",
    }


def get_credit_balance(user_id: int, db: Session) -> Decimal:
    credit = db.query(Credit).filter(Credit.user_id == int(user_id)).first()
    return as_credit_amount(credit.balance if credit else 0)


def has_enough_credits(user_id: int, amount, db: Session) -> bool:
    required = as_credit_amount(amount)
    return required > 0 and get_credit_balance(user_id, db) >= required


def _source_transaction(db: Session, source_type: Optional[str], source_id: Optional[str]) -> Optional[CreditTransaction]:
    if not source_type or not source_id:
        return None
    return (
        db.query(CreditTransaction)
        .filter(
            CreditTransaction.source_type == source_type,
            CreditTransaction.source_id == str(source_id),
        )
        .first()
    )


def grant_credits(
    db: Session,
    *,
    user_id: int,
    amount,
    source_type: Optional[str],
    source_id: Optional[str],
    description: str = "",
) -> bool:
    credit_amount = as_credit_amount(amount)
    if credit_amount <= 0:
        return False
    if _source_transaction(db, source_type, source_id):
        return False

    credit = _get_or_create_credit(db, int(user_id), for_update=True)
    credit.balance = as_credit_amount(credit.balance) + credit_amount
    credit.total_bought = as_credit_amount(credit.total_bought) + credit_amount
    tx = CreditTransaction(
        user_id=int(user_id),
        amount=credit_amount,
        balance_after=as_credit_amount(credit.balance),
        transaction_type="purchase",
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        item_type="credits",
        item_key=None,
        description=description[:200] if description else None,
        created_at=datetime.utcnow(),
    )
    db.add(tx)
    db.flush()
    return True


def consume_credits(
    db: Session,
    *,
    user_id: int,
    amount,
    source_type: Optional[str],
    source_id: Optional[str],
    item_type: str,
    item_key: str,
    description: str = "",
) -> bool:
    credit_amount = as_credit_amount(amount)
    if credit_amount <= 0:
        return False
    if _source_transaction(db, source_type, source_id):
        return False

    credit = _get_or_create_credit(db, int(user_id), for_update=True)
    balance = as_credit_amount(credit.balance)
    if balance < credit_amount:
        raise InsufficientCreditsError(credit_amount, balance)
    credit.balance = balance - credit_amount
    credit.total_used = as_credit_amount(credit.total_used) + credit_amount
    tx = CreditTransaction(
        user_id=int(user_id),
        amount=-credit_amount,
        balance_after=as_credit_amount(credit.balance),
        transaction_type="usage",
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        item_type=item_type[:32] if item_type else None,
        item_key=item_key[:64] if item_key else None,
        description=description[:200] if description else None,
        created_at=datetime.utcnow(),
    )
    db.add(tx)
    db.flush()
    return True
