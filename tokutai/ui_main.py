"""受付簿のメイン画面とトレイ常駐。"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime

from PySide6.QtCore import QDate, QItemSelectionModel, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)
from sqlalchemy import select

from . import __version__, db, service, updater
from .corporate import CompanyNormalizer, load_table
from . import paths, settings as settings_mod
from .bizdays import add_business_days
from .db import Case
from .graph import GraphClient
from .ui_settings import SettingsDialog

log = logging.getLogger(__name__)

COLUMNS = ["状態", "警告", "経過日数", "案件番号", "会社名", "案件作成日", "書類到着日", "不備連絡日", "処理済日",
           "取りやめ日", "備考"]
COL_STATUS, COL_NO = COLUMNS.index("状態"), COLUMNS.index("案件番号")
RED_BG = QColor(255, 205, 205)
GRAY_FG = QColor(130, 130, 130)
WAKE_TICK_SEC = 30  # スリープ復帰を見張る周期
WAKE_GAP_SEC = 180  # 周期がこれ以上空いたら、スリープしていたとみなす
HOLD_BG =QColor(255, 244, 204)  # 保留中（事業所に連絡済みで待っている）


def _icon(color: str) -> QIcon:
    """状態の色の丸に、白い「退」を重ねたアイコン。"""
    pm = QPixmap(64, 64)  # 縮小表示でもぼやけにくいよう、大きめに描く
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 60, 60)
    font = QFont("Yu Gothic UI")
    font.setPixelSize(44)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("white"))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "退")
    p.end()
    return QIcon(pm)


class _Signals(QObject):
    done = Signal(object)


class _SyncTask(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            res = self.fn()
        except Exception as e:  # noqa: BLE001
            log.exception("同期で予期しないエラー")
            res = service.SyncResult(errors=[f"予期しないエラー: {e}"])
        self.signals.done.emit(res)


def _fmt(d: date | None) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


class DateDialog(QDialog):
    def __init__(self, title: str, label: str, parent=None, initial: date | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.edit = QDateEdit(QDate.currentDate() if initial is None else QDate(initial.year, initial.month, initial.day))
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("yyyy-MM-dd")
        form = QFormLayout(self)
        form.addRow(label, self.edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def value(self) -> date:
        return self.edit.date().toPython()


class CancelDialog(QDialog):
    """案件を取りやめにする（退職をやめたなど）。日付と、任意の理由を聞く。"""

    def __init__(self, count: int, skipped: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("取りやめにする")
        self.setMinimumWidth(420)
        self.edit = QDateEdit(QDate.currentDate())
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("yyyy-MM-dd")
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("例: 退職取りやめ（空欄でもかまいません）")
        form = QFormLayout(self)
        msg = f"{count}件を取りやめにします。警告・催促・経過日数の対象から外れます。"
        if skipped:
            msg += f"\n（処理済・取りやめ済の{skipped}件は対象外です）"
        form.addRow(QLabel(msg))
        form.addRow("取りやめ日", self.edit)
        form.addRow("理由（備考に残る）", self.reason)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def value(self) -> date:
        return self.edit.date().toPython()


class HoldDialog(QDialog):
    """事業所に連絡済みで待つ案件を、n営業日後まで保留にする。メモは任意で、備考に残る。"""

    def __init__(self, count: int, skipped: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("保留にする")
        self.setMinimumWidth(420)
        self.days = QComboBox()
        today = date.today()
        for n in range(1, service.HOLD_MAX_DAYS + 1):
            self.days.addItem(f"{n}営業日後（{add_business_days(today, n):%Y-%m-%d}）", n)
        self.days.setCurrentIndex(2)  # 初期値は3営業日後
        self.memo = QLineEdit()
        self.memo.setPlaceholderText("例: ○日に送ると連絡あり（空欄でもかまいません）")
        form = QFormLayout(self)
        msg = f"{count}件を保留にします。確認日までは、警告・催促の対象から外れ、確認日に警告へ戻ります。"
        if skipped:
            msg += f"\n（処理済・取りやめ済の{skipped}件は対象外です）"
        lab = QLabel(msg)
        lab.setWordWrap(True)
        form.addRow(lab)
        form.addRow("確認日", self.days)
        form.addRow("メモ（備考に残る）", self.memo)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def value(self) -> int:
        return int(self.days.currentData())


class EditDatesDialog(QDialog):
    """日付の入れ間違いを、その場で直す。チェックを外すと、その日付は空になる。"""
    FIELDS = (("received_date", "案件作成日"), ("defect_date", "不備連絡日"),
              ("docs_date", "書類到着日"), ("done_date", "処理済日"), ("cancel_date", "取りやめ日"))

    def __init__(self, case: Case, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"日付を修正 {case.case_no}")
        form = QFormLayout(self)
        self._rows: dict[str, tuple[QCheckBox, QDateEdit]] = {}
        for attr, label in self.FIELDS:
            cur = getattr(case, attr)
            chk = QCheckBox("あり"); chk.setChecked(cur is not None)
            ed = QDateEdit(QDate(cur.year, cur.month, cur.day) if cur else QDate.currentDate())
            ed.setCalendarPopup(True)
            ed.setDisplayFormat("yyyy-MM-dd")
            ed.setEnabled(cur is not None)
            chk.toggled.connect(ed.setEnabled)
            row = QHBoxLayout(); row.addWidget(chk); row.addWidget(ed, 1)
            form.addRow(label, row)
            self._rows[attr] = (chk, ed)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self) -> dict[str, date | None]:
        return {a: (ed.date().toPython() if chk.isChecked() else None)
                for a, (chk, ed) in self._rows.items()}


class AddCaseDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("案件を追加")
        self.no = QLineEdit()
        self.company = QLineEdit()
        self.recv = QDateEdit(QDate.currentDate())
        self.recv.setCalendarPopup(True)
        self.recv.setDisplayFormat("yyyy-MM-dd")
        form = QFormLayout(self)
        form.addRow("案件番号", self.no)
        form.addRow("会社名", self.company)
        form.addRow("案件作成日", self.recv)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class ReminderDialog(QDialog):
    """保険会社への催促メールを、送る前に確認するダイアログ。"""

    SEND, SKIP = "send", "skip"

    def __init__(self, reminders: list[service.Reminder], to: list[str], st, parent=None):
        super().__init__(parent)
        self.setWindowTitle("保険会社へ催促メールを送りますか？")
        self.resize(720, 560)
        self.st = st
        self.action: str | None = None  # 「後で」や×で閉じたときは None（記録せず、次の機会に再確認）
        self.reminders = reminders

        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"書類が未着の案件です。送る案件にチェックを入れてください。\n宛先: {', '.join(to)}"))
        self.list = QListWidget()
        for r in reminders:
            it = QListWidgetItem(f"{r.case_no}　{r.company or '（会社名なし）'}　案件作成日 {r.received_date:%Y-%m-%d}　{r.days}営業日")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(it)
        self.list.itemChanged.connect(self._refresh)
        v.addWidget(self.list, 1)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        v.addWidget(self.preview, 1)

        row = QHBoxLayout()
        self.btn_send = QPushButton("送信")
        self.btn_send.clicked.connect(lambda: self._done(self.SEND))
        btn_skip = QPushButton("今回は送らない")
        btn_skip.clicked.connect(lambda: self._done(self.SKIP))
        btn_later = QPushButton("後で")
        btn_later.clicked.connect(self.reject)
        row.addStretch()
        for b in (self.btn_send, btn_skip, btn_later):
            row.addWidget(b)
        v.addLayout(row)
        self._refresh()

    def checked(self) -> list[service.Reminder]:
        return [r for i, r in enumerate(self.reminders) if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def unchecked(self) -> list[service.Reminder]:
        return [r for r in self.reminders if r not in self.checked()]

    def _refresh(self, *_args) -> None:
        sel = self.checked()
        self.btn_send.setEnabled(bool(sel))
        if sel:
            subject, body = service.build_reminder_mail(sel, self.st)
            self.preview.setPlainText(f"件名: {subject}\n\n{body}")
        else:
            self.preview.setPlainText("送る案件が選ばれていません。")

    def _done(self, action: str) -> None:
        self.action = action
        self.accept()


class UpdateDialog(QDialog):
    """新しい版の案内。変更点を見せて、「更新する」か「後で」を選んでもらう。"""

    def __init__(self, info: updater.UpdateInfo, can_install: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新しい版があります")
        self.resize(640, 480)
        self.install = False
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"新しい版 {info.version} があります（今の版: {__version__}）。\n変更点:"))
        notes = QPlainTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(info.notes or "（変更点の記載はありません）")  # 外部の文章なので、書式なしで表示する
        v.addWidget(notes, 1)
        row = QHBoxLayout()
        row.addStretch()
        if can_install:
            v.insertWidget(1, QLabel("「更新する」を押すと、アプリを終了して更新し、自動で起動し直します。"
                                     "受付簿や設定は、そのまま引き継がれます。"))
            go = QPushButton("更新する")
            go.clicked.connect(self._go)
            row.addWidget(go)
            later = QPushButton("後で")
            later.clicked.connect(self.reject)
            row.addWidget(later)
        else:
            v.insertWidget(1, QLabel("このアプリはインストール版ではないため、自動更新は使えません。"))
            close = QPushButton("閉じる")
            close.clicked.connect(self.reject)
            row.addWidget(close)
        v.addLayout(row)

    def _go(self) -> None:
        self.install = True
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, start_minimized: bool = False):
        super().__init__()
        self.setWindowTitle("特退金脱退 処理漏れ防止")
        self.resize(1200, 640)
        self.st = settings_mod.load()
        self.norm = CompanyNormalizer(load_table(paths.ensure_abbr_file()))
        self._client: GraphClient | None = None
        self._client_key: tuple[str, str] | None = None
        self._lock = threading.Lock()
        self._known_alerts: set[tuple[str, str]] = set()
        self._first_check = True
        self._running = False
        self._alert_count = 0
        self._last_error: list[str] = []
        self._update: updater.UpdateInfo | None = None  # 見つかった新しい版
        self._update_prompted: str | None = None  # 案内のダイアログを出した版（同じ版では、1回だけ）
        self._updating = False  # 更新のインストーラーを取得している間
        self._reminder_open = False  # 催促メールの確認ダイアログを開いている間
        self._auth_required = False  # サインイン切れ中
        self._auth_dialog_date: date | None = None  # サインイン切れのダイアログを出した日（1日1回）
        self._totals = {"new": 0, "done": 0, "notified": 0}
        self._last_update = "-"
        self.undo_stack = service.UndoStack()

        self._build_ui()
        self._build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self._apply_timer()
        self.reload()
        if not start_minimized:
            self.show_front()
        QTimer.singleShot(1500, self.sync)  # 起動直後に1回実行
        self.update_timer = QTimer(self)  # 新しい版の確認は、起動の少しあとと、1日1回
        self.update_timer.timeout.connect(self.check_update)
        self.update_timer.start(24 * 60 * 60 * 1000)
        QTimer.singleShot(8000, self.check_update)
        # スリープからの復帰を見つける: 見張りの周期が大きく空いて、日付が変わっていたら、画面を出す
        self._last_tick = datetime.now()
        self.wake_timer = QTimer(self)
        self.wake_timer.timeout.connect(self._wake_check)
        self.wake_timer.start(WAKE_TICK_SEC * 1000)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        w = QWidget()
        self.setCentralWidget(w)
        v = QVBoxLayout(w)

        self.update_bar = QWidget()  # 新しい版があるときだけ表示する
        hb = QHBoxLayout(self.update_bar)
        hb.setContentsMargins(0, 0, 0, 0)
        self.update_label = QLabel()
        self.update_label.setStyleSheet("background:#1f6fd0;color:white;font-weight:bold;padding:8px;")
        self.update_btn = QPushButton("更新する")
        self.update_btn.clicked.connect(self._ask_update)
        hb.addWidget(self.update_label, 1)
        hb.addWidget(self.update_btn)
        self.update_bar.hide()
        v.addWidget(self.update_bar)

        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.banner)

        top = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(service.FILTERS)
        if self.st.view_filter in service.FILTERS:
            self.filter.setCurrentText(self.st.view_filter)
        self.filter.currentIndexChanged.connect(self._filter_changed)
        self.search = QLineEdit()
        self.search.setPlaceholderText("案件番号・会社名・備考で検索（検索中は処理済も表示）")
        self.search.textChanged.connect(self.reload)
        top.addWidget(QLabel("表示:"))
        top.addWidget(self.filter)
        top.addWidget(self.search, 1)
        v.addLayout(top)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._header_clicked)
        self._show_sort_indicator()
        self.table.setColumnWidth(COLUMNS.index("会社名"), 240)
        self.table.setColumnWidth(COL_STATUS, 165)  # 「不備（補正待ち）」が入る幅
        self.table.setColumnWidth(COLUMNS.index("警告"), 90)
        self.table.setColumnWidth(COLUMNS.index("経過日数"), 85)
        self.table.setColumnWidth(COL_NO, 160)
        for name in ("案件作成日", "不備連絡日", "書類到着日", "処理済日", "取りやめ日"):  # yyyy-MM-dd が見切れない幅
            self.table.setColumnWidth(COLUMNS.index(name), 130)
        self._restore_column_order()
        hh.setSectionsMovable(True)  # 見出しのドラッグで列の順番を変えられる
        hh.sectionMoved.connect(self._save_column_order)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_dates())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        v.addWidget(self.table, 1)

        # よく使う操作だけをボタンにし、選んだ案件の状態に合わせて使えるものだけ有効にする。
        # それ以外は「その他」にまとめる（右クリックでも同じ操作ができる）。
        hint = QLabel("案件の行を選んで操作します（Ctrl・Shiftキーで複数選択すると、まとめて処理できます）。ダブルクリックで日付の修正、右クリックでメニューが開きます。")
        hint.setStyleSheet("color:#666;")
        v.addWidget(hint)
        btns = QHBoxLayout()
        self.btn_arrived = QPushButton("書類が届いた"); self.btn_arrived.clicked.connect(self.mark_arrived)
        self.btn_defect = QPushButton("不備を連絡した"); self.btn_defect.clicked.connect(self.mark_defect)
        self.btn_done = QPushButton("処理済にする"); self.btn_done.clicked.connect(self.toggle_done)
        self.btn_undo = QPushButton("元に戻す"); self.btn_undo.clicked.connect(self.undo_last)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo_last)
        self.btn_sync = QPushButton("今すぐ更新"); self.btn_sync.clicked.connect(self.sync)
        self.btn_more = QPushButton("その他")
        more = QMenu(self.btn_more)
        for item in (
            ("案件を追加", self.add_case), None,
            ("保留にする（連絡済み・待つ）", self.hold_case), ("保留を解除", self.release_hold), None,
            ("取りやめにする", self.cancel_case), ("取りやめを取り消す", self.restore_cancelled), None,
            ("書類到着を取り消す", self.cancel_arrived), ("不備を取り消す", self.cancel_defect), None,
            ("日付を修正", self.edit_dates), ("会社名を編集", self.edit_company), ("備考を編集", self.edit_note),
        ):
            if item is None:
                more.addSeparator()
            else:
                more.addAction(item[0], item[1])
        self.btn_more.setMenu(more)
        for b in (self.btn_arrived, self.btn_defect, self.btn_done):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(self.btn_undo)
        btns.addWidget(self.btn_sync)
        btns.addWidget(self.btn_more)
        v.addLayout(btns)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self._build_menubar()

    def _build_menubar(self) -> None:
        """上部のメニュー。案件の操作は「その他」、ファイルの出力・設定・ヘルプはここにまとめる。"""
        bar = self.menuBar()
        m_file = bar.addMenu("ファイル(&F)")
        m_file.addAction("CSV出力", self.export_csv)
        m_file.addAction("設定", self.open_settings)
        m_file.addSeparator()
        m_file.addAction("終了", self.quit_app)
        m_help = bar.addMenu("ヘルプ(&H)")
        m_help.addAction("使い方", self.show_manual)
        m_help.addAction("バージョン情報", self.show_about)

    def show_manual(self) -> None:
        path = paths.manual_path()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            QMessageBox.warning(self, "使い方", f"操作マニュアルが見つかりません。\n{path}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("使い方（操作マニュアル）")
        dlg.resize(760, 560)
        lay = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setOpenExternalLinks(False)  # マニュアル内のリンクから、外部へは開かない
        view.setMarkdown(text)
        lay.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def show_about(self) -> None:
        QMessageBox.information(
            self, "バージョン情報",
            f"特退金脱退 処理漏れ防止\nバージョン {__version__}\n\nデータの場所: {paths.data_dir()}")

    def _build_tray(self) -> None:
        self.icon_ok, self.icon_warn = _icon("#2e9e4f"), _icon("#d62f2f")
        if paths.app_icon_path().exists():  # ウィンドウ・タスクバーはアプリのアイコン、トレイは状態の色の丸
            self.setWindowIcon(QIcon(str(paths.app_icon_path())))
        else:
            self.setWindowIcon(self.icon_ok)
        self.tray = QSystemTrayIcon(self.icon_ok, self)
        menu = QMenu()
        a_open = QAction("開く", self); a_open.triggered.connect(self.show_front)
        a_sync = QAction("今すぐ更新", self); a_sync.triggered.connect(self.sync)
        a_quit = QAction("終了", self); a_quit.triggered.connect(self.quit_app)
        for a in (a_open, a_sync):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(a_quit)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_front() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip("特退金脱退 処理漏れ防止")
        self.tray.show()

    def _center_on_screen(self) -> None:
        area = QApplication.primaryScreen().availableGeometry()
        w, h = min(self.width(), area.width()), min(self.height(), area.height())
        self.resize(w, h)
        self.move(area.x() + (area.width() - w) // 2, area.y() + (area.height() - h) // 2)

    def _wake_check(self, now: datetime | None = None) -> None:
        """スリープから復帰して、日付が変わっていたら、画面を前面に出す（起きたままの日またぎでは出さない）。"""
        now = now or datetime.now()
        slept = (now - self._last_tick).total_seconds() > WAKE_GAP_SEC
        crossed = now.date() != self._last_tick.date()
        self._last_tick = now
        if slept and crossed:
            self.reload()
            self.show_front()
            QTimer.singleShot(10_000, self.sync)  # 復帰直後は通信できないことがあるので、少し待って取り込む

    def show_front(self) -> None:
        self._center_on_screen()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e) -> None:  # ウィンドウを閉じても常駐する
        e.ignore()
        self.hide()

    def quit_app(self) -> None:
        self.tray.hide()
        QApplication.instance().quit()

    # ---------- データ表示 ----------
    def _selection(self) -> list[tuple[str, str]]:
        """選んでいる案件（案件番号, 状態）を、一覧の上から順に返す。"""
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [(self.table.item(r, COL_NO).text(), self.table.item(r, COL_STATUS).text()) for r in rows]

    def _selected_no(self) -> str | None:
        """1件だけ選んでいるときの案件番号。0件・複数件のときは案内を出して None。"""
        sel = self._selection()
        if not sel:
            QMessageBox.information(self, "案件の選択", "一覧で案件を選んでください。")
            return None
        if len(sel) > 1:
            QMessageBox.information(self, "案件の選択", "この操作は、案件を1件だけ選んで行ってください。")
            return None
        return sel[0][0]

    def reload(self) -> None:
        today = date.today()
        with db.session() as s:
            cases = list(s.scalars(select(Case)))
        alerts = {a.case_no: a for a in service.collect_alerts(cases, today, self.st)}
        rows = service.select_rows(cases, alerts, self.filter.currentText(), self.search.text(),
                                   self.st.sort_column, self.st.sort_ascending, today, self.norm.formal_words)
        keep = {no for no, _ in self._selection()}  # 並びが変わっても、同じ案件の選択を保つ
        self.table.setRowCount(len(rows))
        for i, c in enumerate(rows):
            a = alerts.get(c.case_no)
            ed = service.elapsed_days(c, today)
            hold = service.is_on_hold(c, today)
            warn = a.kind if a else (f"保留中（〜{c.hold_until:%m/%d}）" if hold else "")
            vals = {"状態": c.status, "警告": warn, "経過日数": "" if ed is None else str(ed),
                    "案件番号": c.case_no, "会社名": c.company, "案件作成日": _fmt(c.received_date),
                    "不備連絡日": _fmt(c.defect_date), "書類到着日": _fmt(c.docs_date),
                    "処理済日": _fmt(c.done_date), "取りやめ日": _fmt(c.cancel_date), "備考": c.note}
            for j, name in enumerate(COLUMNS):
                it = QTableWidgetItem(vals[name])
                it.setToolTip(vals[name])  # 列に収まらない長い会社名・備考も、マウスを乗せると全文が見える
                if a:
                    it.setBackground(QBrush(RED_BG))
                    it.setForeground(QBrush(QColor(0, 0, 0)))
                elif hold:
                    it.setBackground(QBrush(HOLD_BG))
                    it.setForeground(QBrush(QColor(0, 0, 0)))
                elif c.done_date or c.cancel_date:
                    it.setForeground(QBrush(GRAY_FG))
                self.table.setItem(i, j, it)
        sm = self.table.selectionModel()
        sm.clearSelection()
        for i, c in enumerate(rows):
            if c.case_no in keep:
                sm.select(self.table.model().index(i, 0),
                          QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self._alert_count = len(alerts)
        if self._auth_required:  # 警告件数より優先して表示する（メールを取り込めていないため）
            n = f"（期限超過 {len(alerts)} 件）" if alerts else ""
            self.banner.setText(f"⚠ サインインが切れています。設定画面の「接続テスト」で再サインインしてください{n}")
            self.banner.setStyleSheet("background:#f0b400;color:black;font-size:18px;font-weight:bold;padding:8px;")
        elif alerts:
            self.banner.setText(f"警告 {len(alerts)} 件")
            self.banner.setStyleSheet("background:#d62f2f;color:white;font-size:18px;font-weight:bold;padding:8px;")
        else:
            self.banner.setText("警告なし")
            self.banner.setStyleSheet("background:#2e9e4f;color:white;font-size:18px;font-weight:bold;padding:8px;")
        self.tray.setIcon(self.icon_warn if (alerts or self._auth_required) else self.icon_ok)
        self._popup_new_alerts(alerts)
        self._update_status()
        self._update_buttons()

    def _filter_changed(self, *_args) -> None:
        self.st.view_filter = self.filter.currentText()
        settings_mod.save(self.st)
        self.reload()

    def _restore_column_order(self) -> None:
        """前回の列の並び順を戻す。保存された内容が今の列と合わないときは無視する。"""
        order = self.st.column_order
        if sorted(order) != sorted(COLUMNS):
            return
        hh = self.table.horizontalHeader()
        for visual, name in enumerate(order):
            hh.moveSection(hh.visualIndex(COLUMNS.index(name)), visual)

    def _save_column_order(self, *_args) -> None:
        hh = self.table.horizontalHeader()
        self.st.column_order = [COLUMNS[hh.logicalIndex(v)] for v in range(len(COLUMNS))]
        settings_mod.save(self.st)

    def _header_clicked(self, col: int) -> None:
        """見出しのクリックで、昇順 → 降順 → 標準の順に切り替える。"""
        name = COLUMNS[col]
        if self.st.sort_column != name:
            self.st.sort_column, self.st.sort_ascending = name, True
        elif self.st.sort_ascending:
            self.st.sort_ascending = False
        else:
            self.st.sort_column, self.st.sort_ascending = "", True
        settings_mod.save(self.st)
        self._show_sort_indicator()
        self.reload()

    def _show_sort_indicator(self) -> None:
        hh = self.table.horizontalHeader()
        if self.st.sort_column in COLUMNS:
            order = (Qt.SortOrder.AscendingOrder if self.st.sort_ascending
                     else Qt.SortOrder.DescendingOrder)
            hh.setSortIndicator(COLUMNS.index(self.st.sort_column), order)
            hh.setSortIndicatorShown(True)
        else:
            hh.setSortIndicatorShown(False)

    def _update_buttons(self) -> None:
        """選んだ案件の状態に合わせて、押せるボタンと文言を切り替える。複数選択では件数を添える。"""
        sel = self._selection()
        arrive, _ = service.bulk_targets(sel, service.BULK_ARRIVED)
        defect, _ = service.bulk_targets(sel, service.BULK_DEFECT)
        done, _ = service.bulk_targets(sel, service.BULK_DONE)
        statuses = {st for _, st in sel}
        multi = len(sel) > 1

        def label(text: str, n: int) -> str:
            return f"{text}（{n}件）" if multi and n else text

        self.btn_arrived.setEnabled(bool(arrive))
        base = "補正書類が届いた" if statuses == {db.STATUS_DEFECT} else "書類が届いた"
        self.btn_arrived.setText(label(base, len(arrive)))
        self.btn_defect.setEnabled(bool(defect))
        self.btn_defect.setText(label("不備を連絡した", len(defect)))
        all_done = bool(sel) and statuses == {db.STATUS_DONE}
        self.btn_done.setEnabled(bool(sel))
        self.btn_done.setText(label("処理済を取り消す", len(sel)) if all_done else label("処理済にする", len(done)))
        last = self.undo_stack.peek_label()
        self.btn_undo.setEnabled(last is not None)
        self.btn_undo.setToolTip(f"直前の操作「{last}」を元に戻す（Ctrl+Z）" if last else "元に戻せる操作はありません")

    def _popup_new_alerts(self, alerts: dict) -> None:
        keys = {(a.case_no, a.kind) for a in alerts.values()}
        new = keys - self._known_alerts
        if new and (self._first_check or True):
            self.tray.showMessage("特退金脱退 警告", f"新しい警告が{len(new)}件あります。",
                                  QSystemTrayIcon.MessageIcon.Warning, 8000)
            if not self.isVisible() or self.isMinimized():  # トレイ常駐中は、画面を前面に出して気づかせる
                self.show_front()
        self._known_alerts = keys
        self._first_check = False

    def _update_status(self) -> None:
        t = self._totals
        err = f"　｜　⚠ {self._last_error[0]}" if self._last_error else ""
        self.status.setText(
            f"最終更新 {self._last_update}　新規 {t['new']}件　処理済 {t['done']}件　通知送信 {t['notified']}件{err}")

    # ---------- 同期 ----------
    def _apply_timer(self) -> None:
        self.timer.start(max(5, self.st.sync_minutes) * 60 * 1000)

    def _get_client(self) -> GraphClient:
        key = (self.st.tenant_id, self.st.client_id)
        if self._client is None or self._client_key != key:
            self._client = GraphClient(*key)
            self._client_key = key
        return self._client

    def sync(self, *_args) -> None:
        if self._running:
            return
        if not self.st.is_connectable():
            self._last_error = ["設定画面で接続情報を入力してください。"]
            self.reload()
            return
        self._running = True
        self.btn_sync.setEnabled(False)

        def job():
            with self._lock:
                return service.run_sync(self.st, self._get_client(), self.norm, interactive=False)

        task = _SyncTask(job)
        task.signals.done.connect(self._sync_done)
        QThreadPool.globalInstance().start(task)

    def _sync_done(self, res: service.SyncResult) -> None:
        self._running = False
        self.btn_sync.setEnabled(True)
        self._totals["new"] += res.new_cases
        self._totals["done"] += res.done_cases
        self._totals["notified"] += res.notified
        self._last_error = res.errors
        # 通信エラーなどで認証状態が分からない回は、直前の状態を保つ（バナーを消さない）
        if res.auth_required or not res.errors:
            self._auth_required = res.auth_required
        if not self._auth_required:
            self._auth_dialog_date = None  # 回復後に再び切れたら、同じ日でも案内し直す
        self._last_update = date.today().strftime("%m/%d ") + __import__("datetime").datetime.now().strftime("%H:%M")
        if res.errors:
            self.tray.showMessage("特退金脱退 エラー", res.errors[0], QSystemTrayIcon.MessageIcon.Critical, 8000)
        self.reload()
        self._notify_auth_required()
        self._ask_reminders()

    def _ask_reminders(self) -> None:
        """書類未着の催促メールの候補があれば、送る前に確認する。送信は、確認で「送信」を押したときだけ。"""
        if self._reminder_open or self._auth_required:
            return
        today = date.today()
        with db.session() as s:
            cands = service.collect_reminders(s, self.st, today)
        if not cands:
            return
        self._reminder_open = True  # ダイアログを開いている間に次の取り込みが来ても、重ねて開かない
        try:
            self.show_front()
            dlg = ReminderDialog(cands, self.st.insurer_emails(), self.st, self)
            if not dlg.exec() or dlg.action is None:
                return
            send = dlg.checked() if dlg.action == ReminderDialog.SEND else []
            skip = [r for r in cands if r not in send]
            try:
                with self._lock, db.session() as s:
                    service.send_reminders(s, self.st, self._get_client(), send, skip, today)
            except Exception as e:  # noqa: BLE001
                log.exception("保険会社への催促メールの送信に失敗")
                QMessageBox.critical(self, "催促メール", f"送信できませんでした。次の機会に、もう一度確認します。\n{e}")
                return
            if send:
                self.statusBar().showMessage(f"保険会社へ催促メールを送信しました（{len(send)}件）。", 8000)
        finally:
            self._reminder_open = False

    def _notify_auth_required(self) -> None:
        """サインイン切れを検知した日に1回、画面を前面に出して案内する。"""
        today = date.today()
        if not self._auth_required or self._auth_dialog_date == today:
            return
        self._auth_dialog_date = today
        self.show_front()
        QMessageBox.warning(
            self, "サインインが切れています",
            "Microsoft 365 のサインインが切れたため、メールを取り込めていません。\n"
            "設定画面を開き、「接続テスト」で再サインインしてください。")

    # ---------- 更新 ----------
    def check_update(self) -> None:
        """新しい版を、裏で確認する。通信に失敗しても、ログに残すだけで、画面には出さない。"""
        if not self.st.check_updates or self._updating:
            return

        def job():
            try:
                return ("ok", updater.check_latest())
            except Exception as e:  # noqa: BLE001
                return ("err", str(e))

        task = _SyncTask(job)
        task.signals.done.connect(self._update_checked)
        QThreadPool.globalInstance().start(task)

    def _update_checked(self, res) -> None:
        kind, val = res
        if kind == "err":
            log.warning("%s", val)
            return
        self._update = val
        self._refresh_update_bar()
        if val is None or self._update_prompted == val.version:
            return
        self._update_prompted = val.version
        if self.isVisible() and not self.isMinimized():
            self._ask_update()
        else:  # 画面が隠れているときは、割り込まずに、吹き出しで知らせる
            self.tray.showMessage("特退金脱退 更新", f"新しい版 {val.version} があります。画面を開いて確認してください。",
                                  QSystemTrayIcon.MessageIcon.Information, 8000)

    def _refresh_update_bar(self) -> None:
        info = self._update
        self.update_bar.setVisible(info is not None)
        if info is None:
            return
        can = updater.is_installed_build()
        self.update_label.setText(f"新しい版 {info.version} があります。" + ("" if can else "（インストール版ではないため、自動更新は使えません）"))
        self.update_btn.setVisible(can)
        self.update_btn.setEnabled(not self._updating)

    def _ask_update(self) -> None:
        info = self._update
        if info is None or self._updating:
            return
        dlg = UpdateDialog(info, updater.is_installed_build(), self)
        if dlg.exec() and dlg.install:
            self._apply_update(info)

    def _apply_update(self, info: updater.UpdateInfo) -> None:
        self._updating = True
        self.update_label.setText(f"新しい版 {info.version} を取得しています…")
        self.update_btn.setEnabled(False)

        def job():
            try:
                return ("ok", updater.download_installer(info))
            except Exception as e:  # noqa: BLE001
                return ("err", str(e))

        task = _SyncTask(job)
        task.signals.done.connect(self._update_downloaded)
        QThreadPool.globalInstance().start(task)

    def _update_downloaded(self, res) -> None:
        self._updating = False
        kind, val = res
        if kind == "ok":
            try:
                updater.launch_installer(val)
            except (OSError, updater.UpdateError) as e:
                kind, val = "err", f"インストーラーを実行できませんでした: {e}"
            else:
                self.quit_app()  # インストーラーが、更新したあとに、アプリを起動し直す
                return
        log.warning("更新に失敗: %s", val)
        self._refresh_update_bar()
        QMessageBox.critical(self, "更新", f"更新できませんでした。しばらくしてから、もう一度試してください。\n{val}")

    # ---------- 操作 ----------
    def _update_cases(self, label: str, nos: list[str], fn) -> None:
        """案件を更新し、元に戻せるよう履歴に残す。"""
        with db.session() as s:
            service.apply_undoable(s, self.undo_stack, label + (f"（{len(nos)}件）" if len(nos) > 1 else ""), nos, fn)
        self.reload()

    def _update_case(self, label: str, case_no: str, fn) -> None:
        self._update_cases(label, [case_no], fn)

    def undo_last(self) -> None:
        with db.session() as s:
            res = service.undo(s, self.undo_stack)
        if res is None:
            return
        label, restored, skipped = res
        self.reload()
        if skipped:
            QMessageBox.warning(
                self, "元に戻す",
                f"「{label}」を{restored}件戻しました。\n{skipped}件は、その後に内容が変わっているため戻していません。")
        else:
            self.statusBar().showMessage(f"「{label}」を元に戻しました。", 6000)

    def _bulk(self, title: str, action: str, label: str, apply) -> None:
        """選んだ案件のうち対象のものに、同じ日付をまとめて記録する。日付は1回だけ聞く。"""
        sel = self._selection()
        if not sel:
            QMessageBox.information(self, "案件の選択", "一覧で案件を選んでください。")
            return
        targets, skipped = service.bulk_targets(sel, action)
        if not targets:
            QMessageBox.information(self, title, "選んだ案件は、すでにこの操作ができない状態です。")
            return
        dlg = DateDialog(title, label, self)
        if not dlg.exec():
            return
        if len(sel) > 1:
            msg = f"{len(targets)}件に、{label} {dlg.value():%Y-%m-%d} を記録します。"
            if skipped:
                msg += f"\n（状態が合わない{skipped}件は対象外です）"
            if QMessageBox.question(self, title, msg) != QMessageBox.StandardButton.Yes:
                return
        d = dlg.value()
        self._update_cases(title, targets, lambda c: apply(c, d))

    def mark_arrived(self) -> None:
        self._bulk("書類が届いた", service.BULK_ARRIVED, "書類到着日", service.record_arrived)

    def cancel_arrived(self) -> None:
        no = self._selected_no()
        if no and QMessageBox.question(self, "到着を取り消す", f"{no} の書類到着日を取り消しますか？") \
                == QMessageBox.StandardButton.Yes:
            self._update_case("書類到着の取り消し", no, lambda c: setattr(c, "docs_date", None))

    def mark_defect(self) -> None:
        self._bulk("不備を連絡した", service.BULK_DEFECT, "不備を相手に連絡した日", service.record_defect)

    def cancel_defect(self) -> None:
        no = self._selected_no()
        if no and QMessageBox.question(self, "不備を取り消す", f"{no} の不備連絡日を取り消しますか？") \
                == QMessageBox.StandardButton.Yes:
            self._update_case("不備の取り消し", no, lambda c: setattr(c, "defect_date", None))

    def cancel_case(self) -> None:
        """選んだ案件を、取りやめにする（退職をやめたなど）。理由は任意で、備考に残る。"""
        sel = self._selection()
        if not sel:
            QMessageBox.information(self, "案件の選択", "一覧で案件を選んでください。")
            return
        targets, skipped = service.bulk_targets(sel, service.BULK_CANCEL)
        if not targets:
            QMessageBox.information(self, "取りやめにする", "選んだ案件は、すでにこの操作ができない状態です（処理済・取りやめ済）。")
            return
        dlg = CancelDialog(len(targets), skipped, self)
        if not dlg.exec():
            return
        d, reason = dlg.value(), dlg.reason.text()
        self._update_cases("取りやめ", targets, lambda c: service.record_cancel(c, d, reason))

    def hold_case(self) -> None:
        """事業所に連絡済みで待つ案件を、n営業日後まで保留にする（警告・催促を止める）。"""
        sel = self._selection()
        if not sel:
            QMessageBox.information(self, "案件の選択", "一覧で案件を選んでください。")
            return
        targets, skipped = service.bulk_targets(sel, service.BULK_HOLD)
        if not targets:
            QMessageBox.information(self, "保留にする", "選んだ案件は、すでにこの操作ができない状態です（処理済・取りやめ済）。")
            return
        dlg = HoldDialog(len(targets), skipped, self)
        if not dlg.exec():
            return
        n, memo, today = dlg.value(), dlg.memo.text(), date.today()
        self._update_cases("保留", targets, lambda c: service.record_hold(c, today, n, memo))

    def release_hold(self) -> None:
        """保留を解除して、警告の対象に戻す。"""
        sel = [no for no, _ in self._selection()]
        if not sel:
            QMessageBox.information(self, "案件の選択", "一覧で案件を選んでください。")
            return
        today = date.today()
        with db.session() as s:
            held = [no for no in sel if (c := s.get(Case, no)) is not None and service.is_on_hold(c, today)]
        if not held:
            QMessageBox.information(self, "保留を解除", "保留中の案件を選んでください。")
            return
        text = f"{held[0]} の保留を解除しますか？" if len(held) == 1 else f"{len(held)}件の保留を解除しますか？"
        if QMessageBox.question(self, "保留を解除", text) == QMessageBox.StandardButton.Yes:
            self._update_cases("保留の解除", held, service.release_hold)

    def restore_cancelled(self) -> None:
        """取りやめにした案件を、元の状態に戻す。"""
        sel = [no for no, st in self._selection() if st == db.STATUS_CANCELLED]
        if not sel:
            QMessageBox.information(self, "取りやめを取り消す", "取りやめにした案件を選んでください。")
            return
        text = f"{sel[0]} の取りやめを取り消しますか？" if len(sel) == 1 else f"{len(sel)}件の取りやめを取り消しますか？"
        if QMessageBox.question(self, "取りやめを取り消す", text) == QMessageBox.StandardButton.Yes:
            today = date.today()
            self._update_cases("取りやめの取り消し", sel, lambda c: service.restore_cancel(c, today))

    def edit_dates(self) -> None:
        no = self._selected_no()
        if not no:
            return
        with db.session() as s:
            dlg = EditDatesDialog(s.get(Case, no), self)
        if dlg.exec():
            vals = dlg.values()
            self._update_case("日付の修正", no, lambda c: [setattr(c, k, v) for k, v in vals.items()])

    def _table_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        if item.row() not in {i.row() for i in self.table.selectionModel().selectedRows()}:
            self.table.setCurrentCell(item.row(), COL_STATUS)
        menu = QMenu(self)
        for b in (self.btn_arrived, self.btn_defect, self.btn_done):
            menu.addAction(b.text(), b.click).setEnabled(b.isEnabled())
        # 上から、よく使う記録 → 保留・取りやめ → 取り消し → 修正・編集（下方）
        for item in (
            None,
            ("保留にする（連絡済み・待つ）", self.hold_case), ("保留を解除", self.release_hold), None,
            ("取りやめにする", self.cancel_case), ("取りやめを取り消す", self.restore_cancelled), None,
            ("書類到着を取り消す", self.cancel_arrived), ("不備を取り消す", self.cancel_defect), None,
            ("日付を修正", self.edit_dates), ("会社名を編集", self.edit_company), ("備考を編集", self.edit_note),
        ):
            if item is None:
                menu.addSeparator()
            else:
                menu.addAction(item[0], item[1])
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "CSV出力", f"tokutai_{date.today():%Y%m%d}.csv", "CSVファイル (*.csv)")
        if not path:
            return
        with db.session() as s:
            cases = list(s.scalars(select(Case)))
        try:
            n = service.export_csv(cases, path)
        except OSError as e:
            QMessageBox.warning(self, "CSV出力", f"書き出せませんでした: {e}")
            return
        QMessageBox.information(self, "CSV出力", f"{n}件を書き出しました。\n（絞り込みに関係なく、すべての案件です）")

    def toggle_done(self) -> None:
        sel = self._selection()
        if sel and all(st == db.STATUS_DONE for _, st in sel):  # 全部が処理済なら、取り消す
            n = len(sel)
            text = f"{sel[0][0]} の処理済を取り消しますか？" if n == 1 else f"{n}件の処理済を取り消しますか？"
            if QMessageBox.question(self, "処理済を取り消す", text) == QMessageBox.StandardButton.Yes:
                self._update_cases("処理済の取り消し", [no for no, _ in sel], lambda c: setattr(c, "done_date", None))
            return
        self._bulk("処理済にする", service.BULK_DONE, "処理済日", lambda c, d: setattr(c, "done_date", d))

    def add_case(self) -> None:
        dlg = AddCaseDialog(self)
        if not dlg.exec():
            return
        no = dlg.no.text().strip().upper()
        if not no:
            QMessageBox.warning(self, "案件を追加", "案件番号を入力してください。")
            return
        with db.session() as s:
            if s.get(Case, no):
                QMessageBox.warning(self, "案件を追加", "同じ案件番号がすでに登録されています。")
                return
            c = Case(case_no=no, company=dlg.company.text().strip(),
                     received_date=dlg.recv.date().toPython(), note="手動追加")
            s.add(c)
            s.commit()
            self.undo_stack.push("案件の追加", {no: (None, service.snapshot(c))})
        self.reload()

    def _edit_text(self, title: str, label: str, attr: str, multiline: bool) -> None:
        no = self._selected_no()
        if not no:
            return
        with db.session() as s:
            cur = getattr(s.get(Case, no), attr) or ""
        if multiline:
            text, ok = QInputDialog.getMultiLineText(self, title, label, cur)
        else:
            text, ok = QInputDialog.getText(self, title, label, text=cur)
        if ok:
            self._update_case(f"{label}の編集", no, lambda c: setattr(c, attr, text.strip()))

    def edit_note(self) -> None:
        self._edit_text("備考を編集", "備考", "note", True)

    def edit_company(self) -> None:
        self._edit_text("会社名を編集", "会社名", "company", False)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.st, self)
        if dlg.exec():
            settings_mod.save(self.st)
            self._apply_timer()
            self._client = None
            self.reload()
            self.sync()
