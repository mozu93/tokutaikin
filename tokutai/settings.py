"""設定の読み書き（settings.json）。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

from . import paths


@dataclass
class Settings:
    tenant_id: str = ""
    client_id: str = ""
    folder_name: str = ""
    kw_create: str = "特退金脱退案件作成"
    kw_done: str = "特退金制度申し込み"
    warn_docs_days: int = 7
    warn_process_days: int = 3
    warn_defect_days: int = 7
    manager_emails: list[str] = field(default_factory=lambda: ["", ""])
    notify_enabled: bool = True
    insurer_email: str = ""  # 書類未着の催促メールの宛先（保険会社）。空なら催促機能は動かない
    remind_days: int = 7  # 催促の間隔（案件作成日からの営業日）
    remind_subject: str = "【特退金脱退】書類未着のご確認（{count}件）"  # {count} は案件の件数に置き換わる
    remind_intro: str = (  # 本文の冒頭。このあとに案件の一覧が自動で入る
        "お世話になっております。\n\n"
        "下記の案件について、案件作成日から日数がたちましたが、書類をまだ確認できておりません。\n"
        "お手数ですが、状況のご確認をお願いいたします。")
    remind_outro: str = ""  # 本文の末尾（案件の一覧のあと）。署名などを入れる
    sync_minutes: int = 30
    initial_days: int = 60
    autostart: bool = True
    autostart_initialized: bool = False
    check_updates: bool = True  # 新しい版を、起動時と1日1回、自動で確認する
    view_filter: str = "未完了のみ"  # 一覧の絞り込み（再起動後も覚える）
    sort_column: str = ""  # 並び替えの列名。空は標準の順
    sort_ascending: bool = True
    column_order: list[str] = field(default_factory=list)  # 列の並び順（空は標準）

    def managers(self) -> list[str]:
        return [m.strip() for m in self.manager_emails if m and m.strip()]

    def insurer_emails(self) -> list[str]:
        """保険会社の宛先。「;」「,」や空白で区切って複数入れてもよい。"""
        import re
        return [m for m in re.split(r"[;,\s]+", self.insurer_email) if m]

    def is_connectable(self) -> bool:
        return bool(self.tenant_id.strip() and self.client_id.strip() and self.folder_name.strip())


def load() -> Settings:
    p = paths.settings_path()
    s = Settings()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        names = {f.name for f in fields(Settings)}
        for k, v in raw.items():
            if k in names:
                setattr(s, k, v)
    if s.sort_column == "受付日":  # 列名の変更（受付日 → 案件作成日）への対応
        s.sort_column = "案件作成日"
    mails = list(s.manager_emails)[:2]
    while len(mails) < 2:
        mails.append("")
    s.manager_emails = mails
    return s


def save(s: Settings) -> None:
    paths.settings_path().write_text(
        json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8"
    )
