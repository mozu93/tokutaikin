"""データベースの接続先（このPCごとの設定）。

既定は、このPCだけで使う SQLite（従来どおり）。複数の職員で共有するときは、PostgreSQL を指定する。
パスワードは connection.json には書かず、Windows では DPAPI で暗号化して別のファイルに保存する。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from . import paths

log = logging.getLogger(__name__)

DB_SQLITE = "sqlite"
DB_POSTGRESQL = "postgresql"


@dataclass
class ConnConfig:
    db_type: str = DB_SQLITE
    host: str = "localhost"
    port: int = 5432
    database: str = "tokutai"
    user: str = ""
    password: str = ""

    @property
    def shared(self) -> bool:
        return self.db_type == DB_POSTGRESQL

    def url(self) -> URL | str:
        """init() に渡す接続先。パスワードに記号があっても壊れないよう、URL オブジェクトで作る。"""
        if not self.shared:
            return f"sqlite:///{paths.db_path()}"
        return URL.create("postgresql+psycopg2", username=self.user, password=self.password,
                          host=self.host, port=int(self.port or 5432), database=self.database)

    def validate(self) -> str | None:
        """入力の不備を、利用者向けの文で返す（問題なければ None）。"""
        if not self.shared:
            return None
        if not (self.host.strip() and self.database.strip() and self.user.strip()):
            return "ホスト名・データベース名・ユーザー名を入力してください。"
        return None


def _path():
    return paths.data_dir() / "connection.json"


def _password_store():
    from msal_extensions import FilePersistence, build_encrypted_persistence
    p = str(paths.data_dir() / "db_password.bin")
    try:
        return build_encrypted_persistence(p)  # Windows では DPAPI で暗号化
    except Exception:  # noqa: BLE001
        log.warning("暗号化ストレージが使えないため、データベースのパスワードを平文で保存します")
        return FilePersistence(p)


def exists() -> bool:
    return _path().exists()


def load() -> ConnConfig:
    c = ConnConfig()
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return c
    names = {f.name for f in fields(ConnConfig)} - {"password"}
    for k, v in raw.items():
        if k in names:
            setattr(c, k, v)
    if c.shared:
        try:
            c.password = _password_store().load() or ""
        except Exception:  # noqa: BLE001
            log.exception("データベースのパスワードを読めませんでした")
    return c


def save(c: ConnConfig) -> None:
    data = asdict(c)
    data.pop("password")
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if c.shared:
        _password_store().save(c.password)


def test(c: ConnConfig) -> None:
    """接続できるか調べる。できなければ例外を送出する。"""
    engine = create_engine(c.url(), connect_args={"connect_timeout": 5} if c.shared else {})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def explain(error: Exception) -> str:
    """接続の失敗を、利用者向けの文にする（パスワードなどの詳細は出さない）。"""
    msg = str(getattr(error, "orig", error)).strip().splitlines()
    first = msg[0] if msg else type(error).__name__
    low = first.lower()
    if "password authentication failed" in low:
        hint = "ユーザー名またはパスワードが違います。"
    elif "does not exist" in low and "database" in low:
        hint = "データベース名が違うか、データベースがまだ作られていません。"
    elif "could not connect" in low or "timeout" in low or "refused" in low or "could not translate" in low:
        hint = "サーバーにつながりません。ホスト名・ポート・サーバーの起動、ネットワークを確かめてください。"
    else:
        hint = "接続できませんでした。"
    return f"{hint}（{first}）"
