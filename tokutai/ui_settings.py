"""設定画面。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from . import autostart
from .graph import GraphClient, GraphError
from .settings import Settings


class NoWheelSpinBox(QSpinBox):
    """マウスのホイールで値を変えない数値ボックス。ホイールは、画面のスクロールに回す。"""

    def wheelEvent(self, event) -> None:
        event.ignore()


class SettingsDialog(QDialog):
    def __init__(self, s: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setMinimumWidth(480)
        self.s = s

        self.tenant = QLineEdit(s.tenant_id)
        self.client = QLineEdit(s.client_id)
        self.folder = QLineEdit(s.folder_name)
        self.kw_create = QLineEdit(s.kw_create)
        self.kw_done = QLineEdit(s.kw_done)
        self.warn_docs = NoWheelSpinBox(); self.warn_docs.setRange(1, 60); self.warn_docs.setValue(s.warn_docs_days)
        self.warn_proc = NoWheelSpinBox(); self.warn_proc.setRange(1, 60); self.warn_proc.setValue(s.warn_process_days)
        self.warn_defect = NoWheelSpinBox(); self.warn_defect.setRange(1, 60); self.warn_defect.setValue(s.warn_defect_days)
        self.mgr1 = QLineEdit(s.manager_emails[0])
        self.mgr2 = QLineEdit(s.manager_emails[1])
        self.notify = QCheckBox("管理職へ警告メールを自動送信する"); self.notify.setChecked(s.notify_enabled)
        self.insurer = QLineEdit(s.insurer_email)
        self.insurer.setPlaceholderText("空欄なら催促は使いません（複数は ; 区切り）")
        self.remind = NoWheelSpinBox(); self.remind.setRange(1, 60); self.remind.setSuffix(" 営業日ごと"); self.remind.setValue(s.remind_days)
        self.r_subject = QLineEdit(s.remind_subject)
        self.r_subject.setToolTip("{count} は、案件の件数に置き換わります。")
        self.r_intro = QPlainTextEdit(s.remind_intro); self.r_intro.setFixedHeight(170)
        self.r_outro = QPlainTextEdit(s.remind_outro); self.r_outro.setFixedHeight(110)
        self.r_outro.setPlaceholderText("署名など（空欄でもかまいません）")
        self.sync_min = NoWheelSpinBox(); self.sync_min.setRange(5, 720); self.sync_min.setSuffix(" 分"); self.sync_min.setValue(s.sync_minutes)
        self.init_days = NoWheelSpinBox(); self.init_days.setRange(1, 365); self.init_days.setSuffix(" 日"); self.init_days.setValue(s.initial_days)
        self.auto = QCheckBox("Windows起動時に自動で起動する"); self.auto.setChecked(s.autostart)
        self.upd = QCheckBox("新しい版を自動で確認する"); self.upd.setChecked(s.check_updates)

        self.resize(740, 600)  # 小さいノートPCでも収まるよう、内容は縦スクロールにする（幅780・高さ600以内）
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        root.addWidget(scroll)
        inner = QVBoxLayout(body)
        for title, rows in (
            ("接続", [("テナントID", self.tenant), ("クライアントID", self.client), ("Outlookのフォルダ名", self.folder)]),
            ("判定（件名キーワード）", [("案件作成メール", self.kw_create), ("処理完了メール", self.kw_done)]),
            ("警告（営業日）", [("書類未着の基準日数", self.warn_docs), ("未処理の基準日数", self.warn_proc),
                                  ("補正待ち（不備連絡後）の基準日数", self.warn_defect)]),
            ("通知", [("管理職1の宛先", self.mgr1), ("管理職2の宛先", self.mgr2), ("", self.notify)]),
            ("保険会社への催促（書類未着）", [("保険会社の宛先", self.insurer), ("催促の間隔", self.remind)]),
            ("催促メールの文面（案件の一覧は自動で入ります）",
             [("件名（{count}=件数）", self.r_subject), ("本文の冒頭", self.r_intro), ("本文の末尾", self.r_outro)]),
            ("動作", [("同期の間隔", self.sync_min), ("初回の取り込み範囲", self.init_days), ("", self.auto), ("", self.upd)]),
        ):
            box = QGroupBox(title)
            form = QFormLayout(box)
            for label, w in rows:
                form.addRow(label, w)
            inner.addWidget(box)

        test = QPushButton("接続テスト")
        test.clicked.connect(self._test)
        row = QHBoxLayout(); row.addWidget(test); row.addStretch()
        root.addLayout(row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _save(self) -> None:
        mails = [self.mgr1.text().strip(), self.mgr2.text().strip()]
        insurer = self.insurer.text().strip()
        for m in mails + Settings(insurer_email=insurer).insurer_emails():
            if m and "@" not in m:
                QMessageBox.warning(self, "設定", f"メールアドレスの形式が正しくありません: {m}")
                return
        s = self.s
        s.insurer_email, s.remind_days = insurer, self.remind.value()
        s.remind_subject = self.r_subject.text().strip()
        s.remind_intro, s.remind_outro = self.r_intro.toPlainText().strip(), self.r_outro.toPlainText().strip()
        s.tenant_id, s.client_id = self.tenant.text().strip(), self.client.text().strip()
        s.folder_name = self.folder.text().strip()
        s.kw_create, s.kw_done = self.kw_create.text().strip(), self.kw_done.text().strip()
        s.warn_docs_days, s.warn_process_days = self.warn_docs.value(), self.warn_proc.value()
        s.warn_defect_days = self.warn_defect.value()
        s.manager_emails = mails
        s.notify_enabled = self.notify.isChecked()
        s.sync_minutes, s.initial_days = self.sync_min.value(), self.init_days.value()
        s.autostart = self.auto.isChecked()
        s.check_updates = self.upd.isChecked()
        try:
            autostart.set_enabled(s.autostart)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "設定", f"自動起動の登録を切り替えられませんでした: {e}")
        self.accept()

    def _test(self) -> None:
        tenant, client, folder = self.tenant.text().strip(), self.client.text().strip(), self.folder.text().strip()
        if not (tenant and client and folder):
            QMessageBox.warning(self, "接続テスト", "テナントID・クライアントID・フォルダ名を入力してください。")
            return
        try:
            gc = GraphClient(tenant, client)
            gc.find_folder_id(folder, interactive=True)
        except GraphError as e:
            QMessageBox.critical(self, "接続テスト", str(e))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "接続テスト", f"予期しないエラー: {e}")
            return
        QMessageBox.information(self, "接続テスト", f"サインインに成功し、フォルダ「{folder}」が見つかりました。")
