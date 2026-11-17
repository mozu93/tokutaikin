"""職員の登録と、担当者マーク（共有のデータベースで使う）。

担当者マークのある職員だけが、メールの取り込み・管理職への通知・保険会社への催促をする。
パスワードは使わず、起動時に自分の名前を選ぶ。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Staff

MAX_NAME = 60


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("名前を入力してください。")
    if len(name) > MAX_NAME:
        raise ValueError(f"名前は{MAX_NAME}文字までです。")
    return name


def list_staff(s: Session, active_only: bool = False) -> list[Staff]:
    q = select(Staff).order_by(Staff.id)
    if active_only:
        q = q.where(Staff.is_active.is_(True))
    return list(s.scalars(q))


def get_by_name(s: Session, name: str) -> Staff | None:
    return s.scalars(select(Staff).where(Staff.name == (name or "").strip())).first()


def add(s: Session, name: str, is_operator: bool = False) -> Staff:
    name = clean_name(name)
    if get_by_name(s, name):
        raise ValueError(f"「{name}」は、すでに登録されています。")
    st = Staff(name=name, is_active=True, is_operator=is_operator)
    s.add(st)
    s.commit()
    return st


def rename(s: Session, staff_id: int, new_name: str) -> None:
    new_name = clean_name(new_name)
    st = s.get(Staff, staff_id)
    if st is None:
        raise ValueError("職員が見つかりません。")
    other = get_by_name(s, new_name)
    if other and other.id != staff_id:
        raise ValueError(f"「{new_name}」は、すでに登録されています。")
    st.name = new_name
    s.commit()


def set_operator(s: Session, staff_id: int, flag: bool) -> None:
    st = s.get(Staff, staff_id)
    if st is not None:
        st.is_operator = bool(flag)
        s.commit()


def set_active(s: Session, staff_id: int, flag: bool) -> None:
    st = s.get(Staff, staff_id)
    if st is not None:
        st.is_active = bool(flag)
        s.commit()


def is_operator(s: Session, name: str) -> bool:
    """この名前の職員が、いま担当者か（使用中で、担当者マークがあるとき）。毎回データベースを見る。"""
    st = get_by_name(s, name)
    return bool(st and st.is_active and st.is_operator)


def operator_names(s: Session) -> list[str]:
    return [x.name for x in list_staff(s, active_only=True) if x.is_operator]
