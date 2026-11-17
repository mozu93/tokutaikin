"""職員の選択・管理、データベースの接続先の設定（共有で使う画面）。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import connection, db, staff


class AddStaffDialog(QDialog):
    """職員の登録。担当者マークは、メールを読み込む人だけに付ける。"""

    def __init__(self, parent=None, operator_default: bool = False):
        super().__init__(parent)
        self.setWindowTitle("職員を登録")
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        form.addRow("名前", self.name)
        lay.addLayout(form)
        self.operator = QCheckBox("担当者にする（メールを読み込む人）")
        self.operator.setChecked(operator_default)
        lay.addWidget(self.operator)
        note = QLabel("担当者マークのある職員だけが、メールの取り込み・管理職への通知・保険会社への催促をします。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def register_staff(parent, operator_default: bool = False) -> str | None:
    """登録の画面を開き、登録できたら、その名前を返す。"""
    dlg = AddStaffDialog(parent, operator_default)
    while dlg.exec():
        try:
            with db.session() as s:
                staff.add(s, dlg.name.text(), dlg.operator.isChecked())
            return staff.clean_name(dlg.name.text())
        except ValueError as e:
            QMessageBox.warning(parent, "職員を登録", str(e))
    return None


class StaffSelectDialog(QDialog):
    """起動時に、自分の名前を選ぶ（パスワードはありません）。"""

    def __init__(self, parent=None, current: str = ""):
        super().__init__(parent)
        self.selected_name: str | None = None
        self.setWindowTitle("職員を選んでください")
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("あなたの名前を選んでください。"))
        self.combo = QComboBox()
        lay.addWidget(self.combo)
        row = QHBoxLayout()
        add = QPushButton("職員を登録…")
        add.clicked.connect(self._add)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self._ok)
        row.addWidget(add)
        row.addStretch()
        row.addWidget(ok)
        lay.addLayout(row)
        self._fill(current)

    def _fill(self, select: str = "") -> None:
        self.combo.clear()
        with db.session() as s:
            for x in staff.list_staff(s, active_only=True):
                self.combo.addItem(f"{x.name}（担当者）" if x.is_operator else x.name, x.name)
        i = self.combo.findData(select)
        if i >= 0:
            self.combo.setCurrentIndex(i)

    def _add(self) -> None:
        with db.session() as s:
            first = not staff.list_staff(s)
        name = register_staff(self, operator_default=first)  # 最初の1人は、担当者にしておくことが多い
        if name:
            self._fill(name)

    def _ok(self) -> None:
        name = self.combo.currentData()
        if not name:
            QMessageBox.information(self, "職員を選ぶ", "職員を選んでください。いなければ「職員を登録」を押してください。")
            return
        self.selected_name = name
        self.accept()


class StaffManageDialog(QDialog):
    """職員の一覧。担当者マークと、使用中（外すと、選ぶ一覧から消える）を切り替える。"""

    COL_NAME, COL_OP, COL_ACTIVE = 0, 1, 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("職員の管理")
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        note = QLabel("担当者にチェックを入れた職員だけが、メールを読み込めます（複数人でもかまいません）。\n"
                      "退職・異動した職員は、「使用中」を外してください。")
        note.setStyleSheet("color:#666;")
        lay.addWidget(note)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名前", "担当者", "使用中"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(self.COL_NAME, 260)
        self.table.setColumnWidth(self.COL_OP, 90)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("職員を追加…")
        add.clicked.connect(self._add)
        ren = QPushButton("名前を変更…")
        ren.clicked.connect(self._rename)
        row.addWidget(add)
        row.addWidget(ren)
        row.addStretch()
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self._load()
        self.table.itemChanged.connect(self._changed)

    def _load(self) -> None:
        self.table.blockSignals(True)
        with db.session() as s:
            rows = staff.list_staff(s)
        self.table.setRowCount(len(rows))
        for i, x in enumerate(rows):
            name = QTableWidgetItem(x.name)
            name.setData(Qt.ItemDataRole.UserRole, x.id)
            self.table.setItem(i, self.COL_NAME, name)
            for col, on in ((self.COL_OP, x.is_operator), (self.COL_ACTIVE, x.is_active)):
                it = QTableWidgetItem()
                it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                self.table.setItem(i, col, it)
        self.table.blockSignals(False)

    def _staff_id(self, row: int) -> int:
        return self.table.item(row, self.COL_NAME).data(Qt.ItemDataRole.UserRole)

    def _changed(self, item: QTableWidgetItem) -> None:
        on = item.checkState() == Qt.CheckState.Checked
        with db.session() as s:
            if item.column() == self.COL_OP:
                staff.set_operator(s, self._staff_id(item.row()), on)
            elif item.column() == self.COL_ACTIVE:
                staff.set_active(s, self._staff_id(item.row()), on)
            else:
                return
            has_operator = bool(staff.operator_names(s))
        if not has_operator:
            QMessageBox.warning(self, "担当者", "担当者がいなくなりました。このままでは、だれもメールを読み込めません。")

    def _add(self) -> None:
        register_staff(self)
        self._load()

    def _rename(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            QMessageBox.information(self, "名前を変更", "職員の行を選んでください。")
            return
        old = self.table.item(r, self.COL_NAME).text()
        new, ok = QInputDialog.getText(self, "名前を変更", "新しい名前", text=old)
        if not ok:
            return
        try:
            with db.session() as s:
                staff.rename(s, self._staff_id(r), new)
        except ValueError as e:
            QMessageBox.warning(self, "名前を変更", str(e))
            return
        self._load()


class ConnectionDialog(QDialog):
    """データベースの接続先。1台で使う（SQLite）か、みんなで共有する（PostgreSQL）かを選ぶ。"""

    def __init__(self, parent=None, cfg: connection.ConnConfig | None = None, initial: bool = False):
        super().__init__(parent)
        cfg = cfg or connection.load()
        self.result_cfg: connection.ConnConfig | None = None
        self.migrate = False
        self.setWindowTitle("データの共有設定")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("受付簿の置き場所を選んでください。"))
        self.r_local = QRadioButton("このPCだけで使う（SQLite）")
        self.r_shared = QRadioButton("複数の職員で共有する（PostgreSQL・社内サーバー）")
        lay.addWidget(self.r_local)
        lay.addWidget(self.r_shared)
        self.area = QWidget()
        form = QFormLayout(self.area)
        self.host = QLineEdit(cfg.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(cfg.port or 5432))
        self.database = QLineEdit(cfg.database)
        self.user = QLineEdit(cfg.user)
        self.password = QLineEdit(cfg.password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("ホスト名 / IPアドレス", self.host)
        form.addRow("ポート番号", self.port)
        form.addRow("データベース名", self.database)
        form.addRow("ユーザー名", self.user)
        form.addRow("パスワード", self.password)
        self.chk_migrate = QCheckBox("このPCにある今までの受付簿を、共有のデータベースへ移す（共有側が空のときだけ）")
        form.addRow(self.chk_migrate)
        lay.addWidget(self.area)
        self.r_shared.toggled.connect(self.area.setEnabled)
        (self.r_shared if cfg.shared else self.r_local).setChecked(True)
        self.area.setEnabled(cfg.shared)
        if not initial:
            note = QLabel("保存すると、アプリを終了します。もう一度起動すると、新しい接続先で動きます。")
            note.setStyleSheet("color:#666;")
            lay.addWidget(note)
        row = QHBoxLayout()
        test = QPushButton("接続テスト")
        test.clicked.connect(self._test)
        ok = QPushButton("保存して開始" if initial else "保存")
        ok.setDefault(True)
        ok.clicked.connect(self._ok)
        row.addWidget(test)
        row.addStretch()
        row.addWidget(ok)
        lay.addLayout(row)

    def _cfg(self) -> connection.ConnConfig:
        if not self.r_shared.isChecked():
            return connection.ConnConfig()
        return connection.ConnConfig(
            db_type=connection.DB_POSTGRESQL, host=self.host.text().strip(), port=self.port.value(),
            database=self.database.text().strip(), user=self.user.text().strip(), password=self.password.text())

    def _test(self, quiet: bool = False) -> bool:
        cfg = self._cfg()
        err = cfg.validate()
        if err:
            QMessageBox.warning(self, "接続テスト", err)
            return False
        try:
            connection.test(cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "接続テスト", f"接続できませんでした。\n{connection.explain(e)}")
            return False
        if not quiet:
            QMessageBox.information(self, "接続テスト", "接続できました。")
        return True

    def _ok(self) -> None:
        cfg = self._cfg()
        if cfg.shared and not self._test(quiet=True):
            return
        self.result_cfg = cfg
        self.migrate = cfg.shared and self.chk_migrate.isChecked()
        self.accept()


def apply_connection(parent, cfg: connection.ConnConfig, migrate: bool) -> bool:
    """接続先を保存する。移行を選んだときは、このPCの受付簿を共有のデータベースへ写してから保存する。"""
    if migrate:
        try:
            counts = db.copy_local_to(cfg.url())
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(parent, "データの共有設定", f"受付簿を移せませんでした。接続先は変えていません。{connection.explain(e)}")
            return False
        if counts:
            QMessageBox.information(parent, "データの共有設定", f"受付簿を移しました（案件 {counts.get('cases', 0)} 件）。")
        else:
            QMessageBox.information(
                parent, "データの共有設定", "共有のデータベースにすでに案件があるか、このPCに移す受付簿が無いため、移していません。")
    connection.save(cfg)
    return True
