"""操作マニュアル用のスクリーンショットを、ダミーデータで作る。

実際のアプリの画面（MainWindow・各ダイアログ）を描画して、docs/manual/img/ に PNG で保存する。
本物の受付簿・設定には触れない（一時フォルダを TOKUTAI_HOME にして動かす）。

使い方:  python -I tools/make_manual_screenshots.py
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

from PySide6.QtCore import QEvent, QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QScrollArea  # noqa: E402

from tokutai import __version__, db, service, settings as settings_mod, updater  # noqa: E402
from tokutai.bizdays import add_business_days, business_days_between  # noqa: E402
from tokutai.db import Case  # noqa: E402
from tokutai.ui_main import (  # noqa: E402
    AddCaseDialog, CancelDialog, DateDialog, EditDatesDialog, HoldDialog, MainWindow,
    ReminderDialog, UpdateDialog,
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

app: QApplication


def bizdays_ago(n: int, today: date) -> date:
    """n 営業日前の日付。"""
    d = today
    while business_days_between(d, today) < n:
        d -= timedelta(days=1)
    return d


def seed(today: date) -> None:
    """架空の会社・案件番号で、いろいろな状態の案件を入れる。"""
    ago = lambda n: bizdays_ago(n, today)  # noqa: E731
    hold_until = add_business_days(today, 3)
    # 案件番号, 会社名, 作成日, 不備連絡日, 書類到着日, 処理済日, 取りやめ日, 保留の確認日, 備考
    rows = [
        ("A1024301", "株式会社サンプル商事", ago(9), None, None, None, None, None, ""),
        ("A1024318", "有限会社みらい工業", ago(8), None, None, None, None, None, ""),
        ("A1024355", "サンプル運輸株式会社", ago(10), ago(8), None, None, None, None, "押印漏れ"),
        ("A1024372", "株式会社あおぞら食品", ago(6), None, ago(4), None, None, None, ""),
        ("A1024447", "株式会社つばさ産業", ago(8), None, None, None, None, hold_until,
         f"保留 〜{hold_until:%Y-%m-%d} 確認：{hold_until:%d}日に送ると連絡あり"),
        ("A1024390", "合同会社ひかり設計", ago(5), None, None, None, None, None, ""),
        ("A1024402", "医療法人さくら会", ago(4), None, ago(1), None, None, None, ""),
        ("A1024417", "株式会社テスト電機", ago(3), None, None, None, None, None, ""),
        ("A1024425", "有限会社やまと建設", ago(2), ago(1), None, None, None, None, "住所の記載不備"),
        ("A1024431", "株式会社グリーン物産", ago(1), None, None, None, None, None, ""),
        ("A1024452", "株式会社なごみ薬局", ago(6), None, None, None, ago(1), None,
         f"取りやめ {ago(1):%Y-%m-%d}：退職取りやめ"),
        ("A1024288", "株式会社ひまわり印刷", ago(12), None, ago(10), ago(8), None, None, ""),
        ("A1024296", "有限会社たかはし製作所", ago(11), None, ago(9), ago(7), None, None, ""),
    ]
    with db.session() as s:
        for no, co, rec, dfc, docs, done, cancel, hold, note in rows:
            s.add(Case(case_no=no, company=co, received_date=rec, defect_date=dfc, docs_date=docs,
                       done_date=done, cancel_date=cancel, hold_until=hold, note=note))
        s.commit()


def save(widget, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    unhover(widget)
    pm = widget.grab()
    pm.save(str(OUT / name), "PNG")
    print(f"  {name}  {pm.width()}x{pm.height()}")


def flush(n: int = 3) -> None:
    for _ in range(n):
        app.processEvents()


def grab_menu(menu: QMenu, name: str) -> None:
    menu.ensurePolished()
    menu.adjustSize()
    menu.show()
    flush()
    save(menu, name)
    menu.hide()


def select_rows(win: MainWindow, case_nos: list[str]) -> None:
    sm = win.table.selectionModel()
    sm.clearSelection()
    win.table.setCurrentItem(None)  # 現在のセルの薄い灰色が写らないように
    for r in range(win.table.rowCount()):
        if win.table.item(r, 3).text() in case_nos:
            sm.select(win.table.model().index(r, 0),
                      QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    flush()


def unhover(widget) -> None:
    """マウスカーソルが乗っている行が、薄く色づいて写らないように、ホバーを解除する。"""
    from PySide6.QtWidgets import QTableWidget
    for t in widget.findChildren(QTableWidget):
        QApplication.sendEvent(t.viewport(), QEvent(QEvent.Type.Leave))
    flush(2)


def crop(win: MainWindow, name: str, top: int, bottom: int | None = None) -> None:
    """ウィンドウの縦の範囲 [top, bottom) だけを切り出して保存する。"""
    unhover(win)
    pm = win.grab()
    bottom = pm.height() if bottom is None else bottom
    pm.copy(0, top, pm.width(), bottom - top).save(str(OUT / name), "PNG")
    print(f"  {name}")


def main() -> int:
    global app
    app = QApplication(sys.argv)
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

    MainWindow.sync = lambda self, *_a: None  # 起動直後のメール取り込みと、新しい版の確認は、通信するので止める
    MainWindow.check_update = lambda self, *_a: None
    win = MainWindow(start_minimized=True)
    win.tray.hide()
    win.tray.showMessage = lambda *a, **k: None  # 吹き出しは出さない
    win._known_alerts = set()
    win._last_update = today.strftime("%m/%d ") + "09:30"
    win._totals = {"new": 3, "done": 1, "notified": 2}
    win._last_error = []
    win.resize(1700, 780)
    win.show()
    flush(5)
    win.reload()
    flush()
    win.timer.stop()
    win.update_timer.stop()

    print("画面を保存します:", OUT)

    def band_top() -> int:  # バナー（青い更新バーがあれば、その上）から切り出す
        w = win.update_bar if win.update_bar.isVisible() else win.banner
        return win.centralWidget().mapTo(win, w.geometry().topLeft()).y() - 6

    def band_bottom() -> int:
        return win.centralWidget().mapTo(win, win.banner.geometry().bottomLeft()).y() + 8

    # 1. メイン画面（警告あり）
    win.table.clearSelection()
    win.table.setCurrentItem(None)
    flush()
    save(win, "main.png")

    # 2. バナー4種
    crop(win, "banner_alert.png", band_top(), band_bottom())
    win._auth_required = True
    win.reload(); flush()
    crop(win, "banner_auth.png", band_top(), band_bottom())
    win._auth_required = False
    info = updater.UpdateInfo(version="1.2.0", notes="・表示の改善\n・不具合の修正",
                              installer_name="TokutaiApp-Setup-1.2.0.exe", installer_url="", sha256_url="")
    win.update_label.setText("新しい版 1.2.0 があります。")
    win.update_bar.show(); flush()
    crop(win, "banner_update.png", band_top(), band_bottom())
    win.update_bar.hide(); flush()
    # 警告なし（緑）: 全案件の作成日を今日にして警告を消して描く
    with db.session() as s:
        for c in s.query(Case).all():
            c.received_date = today
            c.defect_date = None
            if c.done_date is None:
                c.docs_date = None
            c.hold_until = None
        s.commit()
    win.reload(); flush()
    crop(win, "banner_ok.png", band_top(), band_bottom())
    with db.session() as s:  # 元に戻す
        s.query(Case).delete()
        s.commit()
    seed(today)
    win.reload(); flush()

    # 3. 行を選んだ状態（ボタンが有効になる）
    select_rows(win, ["A1024301"])
    save(win, "main_selected.png")
    btn_top = win.centralWidget().mapTo(win, win.btn_arrived.geometry().topLeft()).y() - 10
    crop(win, "buttons.png", btn_top)

    # 4. 複数選択
    select_rows(win, ["A1024301", "A1024318"])
    crop(win, "buttons_multi.png", btn_top)

    # 5. 保留中の案件を選んだ状態（保留を解除できる）
    select_rows(win, ["A1024447"])

    # 6. 表示「警告のみ」と「すべて」
    win.filter.setCurrentText("警告のみ"); flush()
    win.table.clearSelection(); win.table.setCurrentItem(None); flush()
    save(win, "main_alert_only.png")
    win.filter.setCurrentText("すべて"); flush()
    win.table.clearSelection(); win.table.setCurrentItem(None); flush()
    save(win, "main_all.png")
    win.filter.setCurrentText("未完了のみ"); flush()

    # 7. 上部のメニュー（ファイル・ヘルプ）と「その他」
    bar = win.menuBar()
    menus = {m.title(): m for m in bar.findChildren(QMenu)}
    file_menu, help_menu = menus["ファイル(&F)"], menus["ヘルプ(&H)"]
    grab_menu(file_menu, "menu_file.png")
    grab_menu(help_menu, "menu_help.png")
    grab_menu(win.btn_more.menu(), "menu_more.png")
    bar_h = bar.geometry().bottom() + 1
    crop(win, "menubar.png", 0, bar_h)

    # 8. 右クリックメニュー（実際の項目と同じ並び）
    select_rows(win, ["A1024355"])
    ctx = QMenu(win)
    for b in (win.btn_arrived, win.btn_defect, win.btn_done):
        ctx.addAction(b.text()).setEnabled(b.isEnabled())
    for t in (None, "保留にする（連絡済み・待つ）", "保留を解除", None, "取りやめにする", "取りやめを取り消す", None,
              "書類到着を取り消す", "不備を取り消す", None, "日付を修正", "会社名を編集", "備考を編集"):
        ctx.addSeparator() if t is None else ctx.addAction(t)
    grab_menu(ctx, "menu_context.png")

    # 9. トレイのメニュー
    tray_menu = QMenu()
    for t in ("開く", "今すぐ更新"):
        tray_menu.addAction(t)
    tray_menu.addSeparator()
    tray_menu.addAction("終了")
    grab_menu(tray_menu, "menu_tray.png")

    # 10. ダイアログ
    d = DateDialog("書類が届いた", "書類到着日", win)
    d.show(); flush(); save(d, "dialog_date.png"); d.close()

    d = CancelDialog(1, 0, win)
    d.reason.setText("退職取りやめ")
    d.show(); flush(); save(d, "dialog_cancel.png"); d.close()

    d = HoldDialog(1, 0, win)
    d.memo.setText(f"{add_business_days(today, 3):%d}日に送ると連絡あり")
    d.show(); flush(); save(d, "dialog_hold.png"); d.close()

    with db.session() as s:
        case = s.get(Case, "A1024355")
        d = EditDatesDialog(case, win)
    d.show(); flush(); save(d, "dialog_edit_dates.png"); d.close()

    d = AddCaseDialog(win)
    d.no.setText("A1024440"); d.company.setText("株式会社サンプル商事")
    d.show(); flush(); save(d, "dialog_add_case.png"); d.close()

    # 11. 催促メールの確認
    with db.session() as s:
        cands = service.collect_reminders(s, st, today)
    d = ReminderDialog(cands, st.insurer_emails(), st, win)
    d.show(); flush(); save(d, "dialog_reminder.png"); d.close()

    # 12. 新しい版の案内
    d = UpdateDialog(info, True, win)
    d.show(); flush(); save(d, "dialog_update.png"); d.close()

    # 13. サインイン切れの案内（アプリと同じ文面）とバージョン情報
    box = QMessageBox(QMessageBox.Icon.Warning, "サインインが切れています",
                      "Microsoft 365 のサインインが切れたため、メールを取り込めていません。\n"
                      "設定画面を開き、「接続テスト」で再サインインしてください。",
                      QMessageBox.StandardButton.Ok, win)
    box.show(); flush(); save(box, "dialog_auth.png"); box.close()

    box = QMessageBox(QMessageBox.Icon.Information, "バージョン情報",
                      f"特退金脱退 処理漏れ防止\nバージョン {__version__}\n\n"
                      "データの場所: C:\\Users\\（利用者名）\\AppData\\Roaming\\TokutaiApp",
                      QMessageBox.StandardButton.Ok, win)
    box.show(); flush(); save(box, "dialog_about.png"); box.close()

    # 14. 設定画面（上・中・下）
    sd = SettingsDialog(settings_mod.load(), win)
    sd.show(); flush(5)
    bar_ = sd.findChild(QScrollArea).verticalScrollBar()
    total = bar_.maximum()
    for i, frac in enumerate((0.0, 0.5, 1.0), 1):
        bar_.setValue(int(total * frac)); flush(5)
        save(sd, f"settings_{i}.png")
    sd.close()

    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
