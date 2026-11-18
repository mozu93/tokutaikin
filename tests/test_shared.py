"""共有（複数の職員で使う）まわりのテスト。PostgreSQL の代わりに SQLite を「共有」として使う。"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokutai import connection, db, paths, service, settings as settings_mod, staff  # noqa: E402
from tokutai.db import Case  # noqa: E402


@pytest.fixture
def shared_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 'shared.db'}", shared=True)
    yield tmp_path
    db.init(f"sqlite:///{tmp_path / 'after.db'}")  # 後のテストに、共有の状態を残さない


def test_staff_operator_flag(shared_db):
    with db.session() as s:
        a = staff.add(s, " 山田 ", is_operator=True)
        b = staff.add(s, "佐藤")
        assert a.name == "山田" and not b.is_operator
        assert staff.is_operator(s, "山田") and not staff.is_operator(s, "佐藤")
        assert not staff.is_operator(s, "未登録")
        assert staff.operator_names(s) == ["山田"]
        # 担当者マークは、毎回データベースを見るので、外すと、すぐ効く
        staff.set_operator(s, a.id, False)
        assert not staff.is_operator(s, "山田")
        staff.set_operator(s, b.id, True)
        assert staff.is_operator(s, "佐藤")
        # 使用中を外した職員は、担当者マークがあっても、担当者として扱わない
        staff.set_active(s, b.id, False)
        assert not staff.is_operator(s, "佐藤")
        assert [x.name for x in staff.list_staff(s, active_only=True)] == ["山田"]


def test_staff_validation(shared_db):
    with db.session() as s:
        staff.add(s, "山田")
        with pytest.raises(ValueError):
            staff.add(s, "山田")
        with pytest.raises(ValueError):
            staff.add(s, "  ")
        b = staff.add(s, "佐藤")
        with pytest.raises(ValueError):
            staff.rename(s, b.id, "山田")
        staff.rename(s, b.id, "佐藤二郎")
        assert staff.get_by_name(s, "佐藤二郎").id == b.id


def test_shared_settings_roundtrip(shared_db):
    st = settings_mod.load()  # 共有の設定がまだ無いので、このPCの値が、全員の初期値になる
    assert db.is_shared()
    st.warn_docs_days = 11
    st.manager_emails = ["a@example.com", "b@example.com"]
    st.tenant_id = "tenant-local"  # このPCだけの設定
    settings_mod.save(st)
    settings_mod.save_shared(st)

    # このPCのファイルには、共有の設定を書かない
    saved = json.loads(paths.settings_path().read_text(encoding="utf-8"))
    assert saved["tenant_id"] == "tenant-local"
    assert "warn_docs_days" not in saved and "manager_emails" not in saved

    # 別のPCに見立てて、ローカルの設定ファイルを消しても、共有の設定は同じ
    paths.settings_path().unlink()
    other = settings_mod.load()
    assert other.warn_docs_days == 11
    assert other.manager_emails == ["a@example.com", "b@example.com"]
    assert other.tenant_id == ""

    # 他の職員が共有の設定を変えたら、読み直しで取り込める
    other.warn_docs_days = 5
    settings_mod.save_shared(other)
    settings_mod.load_shared(st)
    assert st.warn_docs_days == 5


def test_single_pc_settings_unchanged(tmp_path, monkeypatch):
    """1台用（SQLite）では、これまでどおり、すべての設定をファイルに置く。"""
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    assert not db.is_shared()
    st = settings_mod.load()
    st.warn_docs_days = 9
    settings_mod.save(st)
    assert json.loads(paths.settings_path().read_text(encoding="utf-8"))["warn_docs_days"] == 9
    assert settings_mod.load().warn_docs_days == 9


def test_copy_local_to_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 'tokutai.db'}")
    with db.session() as s:
        s.add(Case(case_no="A001", company="サンプル株式会社", received_date=date(2026, 11, 2)))
        s.add(db.NotifyLog(case_no="A001", kind="docs", notified_on=date(2026, 11, 10)))
        s.add(db.ProcessedMail(mail_id="m1", kind="create", case_no="A001"))
        s.commit()
    dst = f"sqlite:///{tmp_path / 'dst.db'}"
    counts = db.copy_local_to(dst, tmp_path / "tokutai.db")
    assert counts == {"cases": 1, "processed_mails": 1, "notify_log": 1}
    db.init(dst, shared=True)
    with db.session() as s:
        assert s.get(Case, "A001").company == "サンプル株式会社"
    # 共有側に案件があるときは、二重に写さない
    assert db.copy_local_to(dst, tmp_path / "tokutai.db") == {}
    db.init(f"sqlite:///{tmp_path / 'after.db'}")


def test_connection_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    assert not connection.exists() and not connection.load().shared
    cfg = connection.ConnConfig(db_type=connection.DB_POSTGRESQL, host="srv", port=5433,
                                database="tokutai", user="app", password="p@ss:/word")
    assert cfg.validate() is None
    connection.save(cfg)
    assert "p@ss" not in (tmp_path / "connection.json").read_text(encoding="utf-8")  # パスワードは別ファイル
    got = connection.load()
    assert got.shared and got.host == "srv" and got.port == 5433 and got.password == "p@ss:/word"
    # 記号の入ったパスワードでも、接続先の URL を壊さない
    url = cfg.url()
    assert url.password == "p@ss:/word" and url.host == "srv" and url.port == 5433
    assert connection.ConnConfig(db_type=connection.DB_POSTGRESQL).validate()


@pytest.fixture
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_only_operator_reads_mail(shared_db, qt_app, monkeypatch):
    """担当者マークのある職員だけが、メールの取り込み（run_sync）を始める。"""
    from tokutai.ui_main import MainWindow

    with db.session() as s:
        staff.add(s, "担当太郎", is_operator=True)
        staff.add(s, "一般花子")
    st = settings_mod.load()
    st.tenant_id, st.client_id, st.folder_name = "t", "c", "f"
    calls = []

    def fake_sync(*a, **k):
        calls.append(1)
        return service.SyncResult()

    monkeypatch.setattr(service, "run_sync", fake_sync)
    monkeypatch.setattr(MainWindow, "_get_client", lambda self: object())  # 実際の接続はしない
    windows = []  # 画面を、テストの終わりまで保つ

    def run_as(name):
        st.staff_name = name
        settings_mod.save(st)
        w = MainWindow(start_minimized=True)
        windows.append(w)
        w.sync()
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(3000)
        for _ in range(5):
            qt_app.processEvents()
        return w

    w = run_as("一般花子")
    assert not w.can_import() and calls == []
    assert w.btn_sync.text() == "最新の情報に更新"
    assert "共有の受付簿" in w.status.text()
    w = run_as("担当太郎")
    assert w.can_import() and calls == [1]
    assert w.btn_sync.text() == "今すぐ更新"
    assert "担当者" in w.windowTitle()
    # マークを外すと、次の同期から、取り込まない
    with db.session() as s:
        staff.set_operator(s, staff.get_by_name(s, "担当太郎").id, False)
    w.sync()
    qt_app.processEvents()
    assert calls == [1]
