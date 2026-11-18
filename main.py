"""エントリーポイント。 python main.py [--minimized]"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from tokutai import autostart, connection, db, paths, settings as settings_mod, staff as staff_mod
from tokutai.ui_main import MainWindow
from tokutai.ui_staff import ConnectionDialog, StaffSelectDialog, apply_connection

SERVER_NAME = "TokutaiShoribore-single-instance"


def setup_logging() -> None:
    h = RotatingFileHandler(paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[h])


def set_app_identity(app: QApplication) -> None:
    """タスクバーに、Python ではなくこのアプリのアイコンを出す。"""
    try:  # Windows に、このアプリ専用のIDを伝える（これがないと、python.exe のアイコンになる）
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(SERVER_NAME)
    except (AttributeError, OSError):
        pass
    icon = paths.app_icon_path()
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))


def choose_connection() -> connection.ConnConfig | None:
    """データベースの接続先を決める。以前から使っているPCは、従来どおり、このPCの SQLite のまま（何も聞かない）。
    はじめて起動するPC（受付簿がまだ無い）だけ、1台で使うか共有するかを聞く。"""
    if connection.exists() or paths.db_path().exists():
        return connection.load()
    dlg = ConnectionDialog(None, connection.ConnConfig(), initial=True)
    if not dlg.exec() or dlg.result_cfg is None:
        return None
    if not apply_connection(None, dlg.result_cfg, dlg.migrate):
        return None
    return dlg.result_cfg


def open_database(cfg: connection.ConnConfig) -> connection.ConnConfig | None:
    """データベースを開く。つながらないときは、理由を知らせて、接続先を直せるようにする。"""
    while True:
        try:
            if not cfg.shared:
                db.daily_backup()  # 1台用は、移行（列の追加）の前に、その日の最初のバックアップを取る
            db.init(cfg.url(), shared=cfg.shared)
            return cfg
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).exception("データベースを開けません")
            QMessageBox.critical(None, "データベースに接続できません", connection.explain(e))
            dlg = ConnectionDialog(None, cfg, initial=True)
            if not dlg.exec() or dlg.result_cfg is None:
                return None
            if apply_connection(None, dlg.result_cfg, dlg.migrate):
                cfg = dlg.result_cfg


def choose_staff(st: settings_mod.Settings) -> bool:
    """共有のとき、だれが使っているかを決める。前回の職員が、いまも使用中なら、そのまま使う（聞かない）。"""
    with db.session() as s:
        row = staff_mod.get_by_name(s, st.staff_name)
        if row is not None and row.is_active:
            return True
    dlg = StaffSelectDialog(None, st.staff_name)
    if not dlg.exec() or not dlg.selected_name:
        return False
    st.staff_name = dlg.selected_name
    settings_mod.save(st)
    return True


def main() -> int:
    app = QApplication(sys.argv)
    set_app_identity(app)
    app.setQuitOnLastWindowClosed(False)  # トレイに常駐
    app.setStyleSheet(
        "* { font-size: 13pt; }"
        "QPushButton { min-height: 40px; padding: 4px 14px; }"
        "QHeaderView::section { padding: 6px; }"
        "QTableWidget::item { padding: 4px; }"
        "QTableWidget::item:selected { background: #2f6fd0; color: white; }"  # 警告の赤背景でも選択行が分かるように
        "QTableWidget::item:focus { border: none; outline: none; }"  # クリックしたセルの枠を出さない
        "QTableWidget { outline: 0; }"
    )

    # 二重起動の防止: 既存の画面を前面に出して終了
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    setup_logging()
    cfg = choose_connection()
    cfg = open_database(cfg) if cfg else None
    if cfg is None:
        return 0

    st = settings_mod.load()
    if cfg.shared and not choose_staff(st):
        return 0
    if not st.autostart_initialized:  # 初回起動時にスタートアップへ登録
        try:
            autostart.set_enabled(st.autostart)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("自動起動の登録に失敗")
        st.autostart_initialized = True
        settings_mod.save(st)

    if st.autostart_initialized and st.autostart:  # 以前の版の登録（--minimized 付き）を、今の形に直す
        try:
            autostart.refresh()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("自動起動の登録の更新に失敗")

    win = MainWindow(start_minimized="--minimized" in sys.argv)

    def on_connection():
        c = server.nextPendingConnection()
        if c:
            c.readyRead.connect(win.show_front)

    server.newConnection.connect(on_connection)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
