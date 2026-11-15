"""操作マニュアル用のスクリーンショットを、ダミーデータで作る。

実際のアプリの画面（MainWindow・各ダイアログ）を描画して、docs/manual/img/ に PNG で保存する。
本物の受付簿・設定には触れない（一時フォルダを TOKUTAI_HOME にして動かす）。

使い方:  python tools/make_manual_screenshots.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "manual" / "img"

# 本物のデータに触れないよう、アプリを読み込む前に保存先を一時フォルダにする
_tmp = tempfile.mkdtemp(prefix="tokutai_manual_")
os.environ["TOKUTAI_HOME"] = _tmp
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox  # noqa: E402

from tokutai import db, service, settings as settings_mod, updater  # noqa: E402
from tokutai.bizdays import business_days_between  # noqa: E402
from tokutai.db import Case  # noqa: E402
from tokutai.ui_main import (  # noqa: E402
    AddCaseDialog, DateDialog, EditDatesDialog, MainWindow, ReminderDialog, UpdateDialog,
)
from tokutai.ui_settings import SettingsDialog  # noqa: E402

STYLE = (  # main.py と同じ
    "* { font-size: 13pt; }"
    "QPushButton { min-height: 40px; padding: 4px 14px; }"
    "QHeaderView::section { padding: 6px; }"
    "QTableWidget::item { padding: 4px; }"
    "QTableWidget::item:selected { background: #2f6fd0; color: white; }"
    "QTableWidget::item:focus { border: none; outline: none; }"
    "QTableWidget { outline: 0; }"
)


def bizdays_ago(n: int, today: date) -> date:
    """n 営業日前の日付。"""
    d = today
    while business_days_between(d, today) < n:
        d -= timedelta(days=1)
    return d


def seed(today: date) -> None:
    """架空の会社・案件番号で、いろいろな状態の案件を入れる。"""
    ago = lambda n: bizdays_ago(n, today)  # noqa: E731
    rows = [
        # 案件番号, 会社名, 作成日, 不備連絡日, 書類到着日, 処理済日, 備考
        ("A1024301", "株式会社サンプル商事", ago(9), None, None, None, ""),
        ("A1024318", "有限会社みらい工業", ago(8), None, None, None, ""),
        ("A1024355", "サンプル運輸株式会社", ago(10), ago(8), None, None, "押印漏れ"),
        ("A1024372", "株式会社あおぞら食品", ago(6), None, ago(4), None, ""),
        ("A1024390", "合同会社ひかり設計", ago(5), None, None, None, ""),
        ("A1024402", "医療法人さくら会", ago(4), None, ago(1), None, ""),
        ("A1024417", "株式会社テスト電機", ago(3), None, None, None, ""),
        ("A1024425", "有限会社やまと建設", ago(2), ago(1), None, None, "住所の記載不備"),
        ("A1024431", "株式会社グリーン物産", ago(1), None, None, None, ""),
        ("A1024288", "株式会社ひまわり印刷", ago(12), None, ago(10), ago(8), ""),
        ("A1024296", "有限会社たかはし製作所", ago(11), None, ago(9), ago(7), "書類到着日の記録なし"),
    ]
    with db.session() as s:
        for no, co, rec, dfc, docs, done, note in rows:
            if no == "A1024296":
                docs = None
            s.add(Case(case_no=no, company=co, received_date=rec, defect_date=dfc,
                       docs_date=docs, done_date=done, note=note))
        s.commit()


def save(widget, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pm = widget.grab()
    pm.save(str(OUT / name), "PNG")
    print(f"  {name}  {pm.width()}x{pm.height()}")


def flush(app: QApplication, n: int = 3) -> None:
    for _ in range(n):
        app.processEvents()


def grab_menu(app: QApplication, menu: QMenu, name: str) -> None:
    menu.ensurePolished()
    menu.adjustSize()
    menu.show()
    flush(app)
    save(menu, name)
    menu.hide()


def select_rows(win: MainWindow, case_nos: list[str]) -> None:
    from PySide6.QtCore import QItemSelectionModel
    sm = win.table.selectionModel()
    sm.clearSelection()
    win.table.setCurrentItem(None)  # 現在のセルの薄い灰色が写らないように
    for r in range(win.table.rowCount()):
        if win.table.item(r, 3).text() in case_nos:
            sm.select(win.table.model().index(r, 0),
                      QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    flush(win_app)


def main() -> int:
    global win_app
    app = QApplication(sys.argv)
    win_app = app
    app.setStyleSheet(STYLE)
    today = date.today()

    st = settings_mod.Settings(
        tenant_id="00000000-0000-0000-0000-000000000000",
        client_id="11111111-1111-1111-1111-111111111111",
        folder_name="特退金", manager_emails=["manager1@example.com", "manager2@example.com"],
        insurer_email="soudan@example.co.jp", view_filter="未完了のみ", autostart_initialized=True)
    settings_mod.save(st)
    db.init()
    seed(today)

    win = MainWindow(start_minimized=True)
    win.tray.hide()
    win.tray.showMessage = lambda *a, **k: None  # 吹き出しは出さない
    win._known_alerts = set()
    win._last_update = today.strftime("%m/%d ") + "09:30"
    win._totals = {"new": 3, "done": 1, "notified": 2}
    win._last_error = []
    win.resize(1560, 620)
    win.show()
    flush(app, 5)
    win.reload()
    flush(app)
    win.timer.stop()
    win.update_timer.stop()

    print("画面を保存します:", OUT)

    # 1. メイン画面（警告あり）
    win.table.clearSelection()
    win.table.setCurrentItem(None)
    flush(app)
    save(win, "main.png")

    # 2. バナー3種（上部だけ切り出す）
    def top_band(name: str) -> None:
        pm = win.grab()
        h = win.banner.geometry().bottom() + 6
        pm.copy(0, 0, pm.width(), h).save(str(OUT / name), "PNG")
        print(f"  {name}")

    top_band("banner_alert.png")
    win._auth_required = True
    win.reload(); flush(app)
    top_band("banner_auth.png")
    win._auth_required = False
    info = updater.UpdateInfo(version="1.2.0", notes="・表示の改善\n・不具合の修正",
                              installer_name="TokutaiApp-Setup-1.2.0.exe", installer_url="", sha256_url="")
    win.update_label.setText("新しい版 1.2.0 があります。")
    win.update_bar.show(); flush(app)
    top_band("banner_update.png")
    win.update_bar.hide(); flush(app)
    # 警告なし（緑）のバナー: 警告の出る案件を一時的に除いて描画する
    with db.session() as s:
        saved = [(c.case_no, c.received_date) for c in s.query(Case).all()]
        for c in s.query(Case).all():
            c.received_date = today
            c.docs_date = None if c.done_date is None else c.docs_date
            c.defect_date = None
        s.commit()
    win.reload(); flush(app)
    top_band("banner_ok.png")
    with db.session() as s:  # 元に戻す
        s.query(Case).delete()
        s.commit()
    seed(today)
    win.reload(); flush(app)

    # 3. 行を選んだ状態（ボタンが有効になる）
    select_rows(win, ["A1024301"])
    save(win, "main_selected.png")

    # ボタン列だけを切り出す
    pm = win.grab()
    bt = win.btn_arrived.geometry()
    y = win.centralWidget().mapTo(win, bt.topLeft()).y() - 10
    pm.copy(0, y, pm.width(), pm.height() - y).save(str(OUT / "buttons.png"), "PNG")
    print("  buttons.png")

    # 4. 複数選択
    select_rows(win, ["A1024301", "A1024318"])
    pm = win.grab()
    y = win.centralWidget().mapTo(win, win.btn_arrived.geometry().topLeft()).y() - 10
    pm.copy(0, y, pm.width(), pm.height() - y).save(str(OUT / "buttons_multi.png"), "PNG")
    print("  buttons_multi.png")
    select_rows(win, ["A1024301"])

    # 5. 絞り込み（プルダウン）と検索
    win.filter.setCurrentText("警告のみ"); flush(app)
    win.table.clearSelection(); flush(app)
    save(win, "main_alert_only.png")
    win.filter.setCurrentText("未完了のみ"); flush(app)

    # 6. 「その他」メニュー
    grab_menu(app, win.btn_more.menu(), "menu_more.png")

    # 7. 右クリックメニュー
    select_rows(win, ["A1024355"])
    ctx = QMenu(win)
    for b in (win.btn_arrived, win.btn_defect, win.btn_done):
        ctx.addAction(b.text()).setEnabled(b.isEnabled())
    ctx.addSeparator()
    for t in ("日付を修正", "会社名を編集", "備考を編集", "書類到着を取り消す", "不備を取り消す"):
        ctx.addAction(t)
    grab_menu(app, ctx, "menu_context.png")

    # 8. トレイのメニュー
    tray_menu = QMenu()
    for t in ("開く", "今すぐ更新"):
        tray_menu.addAction(t)
    tray_menu.addSeparator()
    tray_menu.addAction("終了")
    grab_menu(app, tray_menu, "menu_tray.png")

    # 9. ダイアログ
    d = DateDialog("書類が届いた", "書類到着日", win)
    d.show(); flush(app); save(d, "dialog_date.png"); d.close()

    with db.session() as s:
        case = s.get(Case, "A1024355")
        d = EditDatesDialog(case, win)
    d.show(); flush(app); save(d, "dialog_edit_dates.png"); d.close()

    d = AddCaseDialog(win)
    d.no.setText("A1024440"); d.company.setText("株式会社サンプル商事")
    d.show(); flush(app); save(d, "dialog_add_case.png"); d.close()

    # 10. 催促メールの確認
    with db.session() as s:
        cands = service.collect_reminders(s, st, today)
    d = ReminderDialog(cands, st.insurer_emails(), st, win)
    d.show(); flush(app); save(d, "dialog_reminder.png"); d.close()

    # 11. 新しい版の案内
    d = UpdateDialog(info, True, win)
    d.show(); flush(app); save(d, "dialog_update.png"); d.close()

    # 12. サインイン切れの案内（アプリの文面で再現）
    box = QMessageBox(win)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("サインインが切れています")
    box.setText("Microsoft 365 のサインインが切れています。\n"
                "「その他」→「設定」→「接続テスト」で、再サインインしてください。")
    box.show(); flush(app); save(box, "dialog_auth.png"); box.close()

    # 13. 設定画面（上・中・下）
    sd = SettingsDialog(settings_mod.load(), win)
    sd.show(); flush(app, 5)
    from PySide6.QtWidgets import QScrollArea
    bar = sd.findChild(QScrollArea).verticalScrollBar()
    total = bar.maximum()
    for i, frac in enumerate((0.0, 0.5, 1.0), 1):
        bar.setValue(int(total * frac)); flush(app, 5)
        save(sd, f"settings_{i}.png")
    sd.close()

    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
