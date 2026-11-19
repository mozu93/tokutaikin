"""SQLite（SQLAlchemy）のモデルとセッション。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from sqlalchemy import Boolean, Date, Integer, String, Text, UniqueConstraint, create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import paths

STATUS_RECEIVED = "書類待ち"
STATUS_ARRIVED = "書類到着"
STATUS_DONE = "処理済"
STATUS_DEFECT = "不備（補正待ち）"
STATUS_CANCELLED = "取りやめ"  # 退職をやめたなど、案件そのものを取りやめた


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"
    case_no: Mapped[str] = mapped_column(String(40), primary_key=True)
    company: Mapped[str] = mapped_column(String(200), default="")
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    docs_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    done_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    defect_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cancel_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    hold_until: Mapped[date | None] = mapped_column(Date, nullable=True)  # 保留の確認日（この日になると警告に戻る）
    note: Mapped[str] = mapped_column(Text, default="")

    @property
    def status(self) -> str:
        if self.done_date:  # 処理完了メールが届いたなど、実際に処理された場合は、取りやめより優先する
            return STATUS_DONE
        if self.cancel_date:
            return STATUS_CANCELLED
        if self.docs_date:
            return STATUS_ARRIVED
        if self.defect_date:
            return STATUS_DEFECT
        return STATUS_RECEIVED


class ProcessedMail(Base):
    __tablename__ = "processed_mails"
    mail_id: Mapped[str] = mapped_column(String(400), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    case_no: Mapped[str] = mapped_column(String(40), default="")


class NotifyLog(Base):
    __tablename__ = "notify_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_no: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20))
    notified_on: Mapped[date] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("case_no", "kind", "notified_on"),)


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Staff(Base):
    """職員。共有（PostgreSQL）で使うときだけ使う。パスワードはなく、起動時に自分を選ぶ。

    is_operator が「担当者」マーク。マークのある職員だけが、メールの取り込み・管理職への通知・
    保険会社への催促をする（ほかの職員は、共有の受付簿を見て、案件を記録する）。
    """
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_operator: Mapped[bool] = mapped_column(Boolean, default=False)


_engine = None
_factory: sessionmaker | None = None
_shared = False  # 複数のPCで共有するデータベース（PostgreSQL）につないでいるか


def is_shared() -> bool:
    return _shared


def _migrate(engine) -> None:
    """既存のDBに、後から増えた列を足す（SQLite・PostgreSQL 共通）。"""
    cols = {c["name"] for c in inspect(engine).get_columns("cases")}
    with engine.begin() as conn:
        for name in ("defect_date", "cancel_date", "hold_until"):
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE cases ADD COLUMN {name} DATE")


def _fix_sequences(engine) -> None:
    """PostgreSQL の自動採番（id）を、表の最大値に合わせる。

    id を指定して行を写すと採番が進まず、次の追加が既存の id と重複するため。
    """
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for table in ("notify_log", "staff"):
            conn.exec_driver_sql(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            )


def _engine_kwargs(url) -> dict:
    """PostgreSQL は、切れた接続を使い回さず、つながらないときは長く待たずに知らせる。"""
    if make_url(url).get_backend_name() == "postgresql":
        return {"pool_pre_ping": True, "connect_args": {"connect_timeout": 5}}
    return {}


def init(url=None, shared: bool | None = None) -> None:
    """データベースを開く。url を省くと、このPCの tokutai.db（1台用）。

    shared は、共有のデータベースか（省くと、接続先が SQLite 以外なら共有）。
    """
    global _engine, _factory, _shared
    if url is None:
        url = f"sqlite:///{paths.db_path()}"
    engine = create_engine(url, future=True, **_engine_kwargs(url))
    Base.metadata.create_all(engine)
    _migrate(engine)
    _fix_sequences(engine)
    _engine = engine
    _shared = engine.dialect.name != "sqlite" if shared is None else shared
    _factory = sessionmaker(_engine, expire_on_commit=False)


def copy_local_to(dst_url, src_path=None) -> dict[str, int]:
    """このPCの受付簿（SQLite）を、共有のデータベースへ写す。共有側に案件がすでにあるときは、何もしない。

    写す内容は、案件・取り込み済みメール・通知履歴。戻り値は、写した件数（写さなかったときは空）。
    """
    src = Path(src_path or paths.db_path())
    if not src.exists():
        return {}
    se = create_engine(f"sqlite:///{src}", future=True)
    de = create_engine(dst_url, future=True, **_engine_kwargs(dst_url))
    try:
        Base.metadata.create_all(de)
        _migrate(de)
        with Session(se) as a, Session(de) as b:
            if b.scalars(select(Case).limit(1)).first() is not None:
                return {}
            counts = {}
            for model in (Case, ProcessedMail, NotifyLog):
                rows = list(a.scalars(select(model)))
                for r in rows:
                    b.add(model(**{c.name: getattr(r, c.name) for c in model.__table__.columns}))
                counts[model.__tablename__] = len(rows)
            b.commit()
            _fix_sequences(de)
            return counts
    finally:
        se.dispose()
        de.dispose()


def session() -> Session:
    if _factory is None:
        init()
    return _factory()  # type: ignore[misc]


def get_meta(s: Session, key: str, default: str = "") -> str:
    m = s.get(Meta, key)
    return m.value if m else default


def set_meta(s: Session, key: str, value: str) -> None:
    m = s.get(Meta, key)
    if m:
        m.value = value
    else:
        s.add(Meta(key=key, value=value))


def daily_backup(keep: int = 30, today: date | None = None) -> bool:
    """1日1回、データベースのコピーを保存する。新しく作ったときだけ True を返す。

    使用中でも整合したコピーが取れるよう、SQLite のバックアップ機能を使う。
    起動時と、同期のたびに呼ぶ（トレイに常駐して再起動しなくても、毎日取れるようにするため）。
    """
    if _shared:  # 共有のデータベースは、サーバー側（管理者）でバックアップする
        return False
    src = _engine.url.database if _engine is not None else str(paths.db_path())
    if not src or not Path(src).exists():
        return False
    dst = paths.backup_dir() / f"tokutai_{(today or date.today()):%Y%m%d}.db"
    made = False
    if not dst.exists():
        tmp = dst.with_suffix(".tmp")
        with closing(sqlite3.connect(src)) as a, closing(sqlite3.connect(tmp)) as b:
            a.backup(b)
        tmp.replace(dst)
        made = True
    for old in sorted(paths.backup_dir().glob("tokutai_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return made
