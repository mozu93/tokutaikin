"""取り込み・警告判定・管理職通知のロジック。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from . import db
from .bizdays import add_business_days, business_days_between
from .corporate import CompanyNormalizer
from .db import Case, NotifyLog, ProcessedMail
from .graph import GraphAuthRequired
from .mailparse import KIND_CREATE, KIND_DONE, classify, html_to_text, parse_body
from .settings import Settings

log = logging.getLogger(__name__)

ALERT_DOCS = "書類未着"
ALERT_PROCESS = "未処理"
ALERT_DEFECT = "補正未着"
NOTE_UNSEEN = "受付メール未確認"
NOTE_DONE_AFTER_CANCEL = "取りやめ後に処理完了メールを受信"
RETENTION_DAYS = 365  # 処理済・取りやめの案件を残す期間


# ---------- 警告判定 ----------
@dataclass(frozen=True)
class Alert:
    case_no: str
    company: str
    kind: str
    base_date: date
    days: int


HOLD_MAX_DAYS = 10  # 保留で選べる最大の営業日数


def is_on_hold(c: Case, today: date) -> bool:
    """保留中か。確認日の前日までが保留で、確認日になると警告に戻る。"""
    return bool(c.hold_until and today < c.hold_until and not c.done_date and not c.cancel_date)


def alert_kind(c: Case, today: date, s: Settings) -> str | None:
    if c.done_date or c.cancel_date:  # 処理済・取りやめの案件は、警告しない
        return None
    if is_on_hold(c, today):  # 事業所に連絡済みで、確認日まで待つ案件は、警告しない
        return None
    if c.docs_date:
        if business_days_between(c.docs_date, today) >= s.warn_process_days:
            return ALERT_PROCESS
        return None
    if c.defect_date:
        if business_days_between(c.defect_date, today) >= s.warn_defect_days:
            return ALERT_DEFECT
        return None
    if c.received_date and business_days_between(c.received_date, today) >= s.warn_docs_days:
        return ALERT_DOCS
    return None


def elapsed_days(c: Case, today: date) -> int | None:
    """状態ごとの経過営業日。書類待ち→案件作成日から、書類到着→到着日から。処理済・取りやめは空。"""
    if c.done_date or c.cancel_date:
        return None
    base = c.docs_date or c.defect_date or c.received_date
    return business_days_between(base, today) if base else None


def collect_alerts(cases: list[Case], today: date, s: Settings) -> list[Alert]:
    out: list[Alert] = []
    for c in cases:
        k = alert_kind(c, today, s)
        if k:
            base = {ALERT_PROCESS: c.docs_date, ALERT_DEFECT: c.defect_date}.get(k, c.received_date)
            out.append(Alert(c.case_no, c.company, k, base, business_days_between(base, today)))  # type: ignore[arg-type]
    return out


# ---------- 一覧の絞り込みと並び替え ----------
FILTER_OPEN = "未完了のみ"
FILTER_ALL = "すべて"
FILTER_ALERT = "警告のみ"
FILTER_DEFECT = "不備のみ"
FILTERS = [FILTER_OPEN, FILTER_ALL, FILTER_ALERT, FILTER_DEFECT]

_STATUS_ORDER = {db.STATUS_RECEIVED: 0, db.STATUS_DEFECT: 1, db.STATUS_ARRIVED: 2, db.STATUS_DONE: 3,
                 db.STATUS_CANCELLED: 4}


def company_sort_key(name: str, words: tuple[str, ...] = ()) -> str:
    """会社名の並び替え用の値。先頭・末尾の法人の種類（株式会社など）を除く。除くと空になるときは、そのまま使う。"""
    s = (name or "").strip()
    core = s
    for w in words:  # 長い語から。「株式会社○○」「○○株式会社」の両方を同じ並びにする
        if core.startswith(w) and len(core) > len(w):
            core = core[len(w):].strip()
            break
    for w in words:
        if core.endswith(w) and len(core) > len(w):
            core = core[:-len(w)].strip()
            break
    return core or s


def _sort_value(col: str, c: Case, alert: Alert | None, today: date, words: tuple[str, ...] = ()):
    """列ごとの並び替え値。空（None）は、昇順でも降順でも最後に回す。"""
    if col == "会社名":
        return company_sort_key(c.company, words) or None
    if col == "状態":
        return _STATUS_ORDER[c.status]
    if col == "警告":
        return alert.kind if alert else None
    if col == "案件番号":
        return c.case_no
    if col == "案件作成日":
        return c.received_date
    if col == "不備連絡日":
        return c.defect_date
    if col == "書類到着日":
        return c.docs_date
    if col == "処理済日":
        return c.done_date
    if col == "取りやめ日":
        return c.cancel_date
    if col == "経過日数":
        return elapsed_days(c, today)
    if col == "備考":
        return c.note or None
    return None


def select_rows(cases: list[Case], alerts: dict[str, Alert], mode: str, query: str,
                sort_col: str, ascending: bool, today: date, corp_words: tuple[str, ...] = ()) -> list[Case]:
    """絞り込み・検索・並び替えをした案件の一覧を返す。

    検索語があるときは「未完了のみ」を無視して、処理済・取りやめも探す（探し漏れ防止）。
    検索は案件番号・会社名・備考が対象。
    """
    q = (query or "").strip().lower()
    rows = []
    for c in cases:
        if mode == FILTER_OPEN and not q and (c.done_date or c.cancel_date):
            continue
        if mode == FILTER_ALERT and c.case_no not in alerts:
            continue
        if mode == FILTER_DEFECT and c.status != db.STATUS_DEFECT:
            continue
        if q and not any(q in (v or "").lower() for v in (c.case_no, c.company, c.note)):
            continue
        rows.append(c)
    if sort_col:
        filled = [(c, _sort_value(sort_col, c, alerts.get(c.case_no), today, corp_words)) for c in rows]
        have = sorted((x for x in filled if x[1] is not None), key=lambda x: (x[1], x[0].case_no),
                      reverse=not ascending)
        rows = [c for c, _ in have] + [c for c, v in filled if v is None]
    else:  # 標準: 警告 → 未完了 → 処理済・取りやめ、案件作成日の古い順
        rows.sort(key=lambda c: (c.case_no not in alerts, bool(c.done_date or c.cancel_date),
                                 c.received_date or date.min))
    return rows


# ---------- 元に戻す ----------
UNDO_LIMIT = 10
_UNDO_FIELDS = ("company", "received_date", "defect_date", "docs_date", "done_date", "cancel_date", "hold_until", "note")


def snapshot(c: Case) -> dict:
    return {f: getattr(c, f) for f in _UNDO_FIELDS}


class UndoStack:
    """画面からの操作を、直前の数回（既定10回）まで戻すための履歴。アプリを閉じると消える。"""

    def __init__(self, limit: int = UNDO_LIMIT) -> None:
        self.limit = limit
        self._items: list[tuple[str, dict[str, tuple[dict | None, dict | None]]]] = []

    def push(self, label: str, changes: dict[str, tuple[dict | None, dict | None]]) -> None:
        """changes: 案件番号 → (操作前, 操作後)。新規作成の操作前は None。"""
        if changes:
            self._items.append((label, changes))
            del self._items[:-self.limit]

    def pop(self):
        return self._items.pop() if self._items else None

    def peek_label(self) -> str | None:
        return self._items[-1][0] if self._items else None

    def __len__(self) -> int:
        return len(self._items)


def apply_undoable(s: Session, stack: UndoStack, label: str, nos: list[str], fn) -> int:
    """案件に fn を適用して保存し、元に戻せるよう履歴へ残す。実際に変わった件数を返す。"""
    changes = {}
    for no in nos:
        c = s.get(Case, no)
        if c is None:
            continue
        before = snapshot(c)
        fn(c)
        after = snapshot(c)
        if before != after:
            changes[no] = (before, after)
    s.commit()
    stack.push(label, changes)
    return len(changes)


def undo(s: Session, stack: UndoStack) -> tuple[str, int, int] | None:
    """直前の操作を戻す。（操作名, 戻した件数, 戻せなかった件数）を返す。履歴が空なら None。

    操作のあとで、同じ案件がメール取り込みなどで変わっていたら、上書きしないよう戻さない。
    """
    item = stack.pop()
    if item is None:
        return None
    label, changes = item
    restored = skipped = 0
    for no, (before, after) in changes.items():
        c = s.get(Case, no)
        if c is None or snapshot(c) != after:
            skipped += 1
            continue
        if before is None:  # 追加した案件は、削除して戻す
            s.delete(c)
        else:
            for k, v in before.items():
                setattr(c, k, v)
        restored += 1
    s.commit()
    return label, restored, skipped


# ---------- まとめて処理 ----------
BULK_ARRIVED, BULK_DEFECT, BULK_DONE, BULK_CANCEL, BULK_HOLD = "arrived", "defect", "done", "cancel", "hold"


def bulk_targets(selection: list[tuple[str, str]], action: str) -> tuple[list[str], int]:
    """選んだ案件（案件番号, 状態）のうち、その操作ができるものと、対象外の件数を返す。

    処理済・取りやめの案件には、到着・不備・処理済・取りやめのどれも記録しない。不備の案件に
    「不備を連絡した」を重ねて記録することもしない（連絡日が上書きされるのを防ぐ）。
    """
    skip = {db.STATUS_DONE, db.STATUS_CANCELLED} | ({db.STATUS_DEFECT} if action == BULK_DEFECT else set())
    targets = [no for no, st in selection if st not in skip]
    return targets, len(selection) - len(targets)


# ---------- CSV出力 ----------
CSV_HEADER = ["状態", "案件番号", "会社名", "案件作成日", "不備連絡日", "書類到着日", "処理済日", "取りやめ日", "保留の確認日", "備考"]


def export_csv(cases: list[Case], path) -> int:
    """案件をCSVに書き出す（Excelで開けるようBOM付きUTF-8）。書き出した件数を返す。"""
    import csv

    def f(d: date | None) -> str:
        return d.strftime("%Y-%m-%d") if d else ""

    rows = sorted(cases, key=lambda c: (c.received_date or date.min, c.case_no))
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(CSV_HEADER)
        for c in rows:
            w.writerow([c.status, c.case_no, c.company, f(c.received_date), f(c.defect_date),
                        f(c.docs_date), f(c.done_date), f(c.cancel_date), f(c.hold_until), c.note])
    return len(rows)


# ---------- 取り込み ----------
class MailSource(Protocol):
    def fetch_messages(self, folder_id: str, since: datetime, interactive: bool = ...) -> list: ...
    def find_folder_id(self, name: str, interactive: bool = ...) -> str: ...


@dataclass
class SyncResult:
    new_cases: int = 0
    done_cases: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)
    auth_required: bool = False  # サインインが切れていて、再サインインが必要


def _append_note(c: Case, note: str) -> None:
    if note and note not in (c.note or ""):
        c.note = f"{c.note}　{note}".strip() if c.note else note


def record_defect(c: Case, defect_date: date) -> None:
    """不備を連絡した日を記録する。元の書類到着日は備考に残す。"""
    if c.docs_date:
        _append_note(c, f"元の書類到着日 {c.docs_date:%Y-%m-%d}")
    c.defect_date = defect_date
    c.docs_date = None  # 補正書類が届くまで、書類は揃っていない扱い
    c.hold_until = None  # 状態が変わったので、保留は解く


def record_arrived(c: Case, docs_date: date) -> None:
    """書類が届いた日を記録する。不備は解消し、保留は解く。"""
    c.docs_date = docs_date
    c.defect_date = None  # 補正書類が届いたので不備は解消
    c.hold_until = None  # 状態が変わったので、保留は解く


def record_hold(c: Case, today: date, business_days: int, memo: str = "") -> date:
    """事業所に連絡済みで待つ案件を、n営業日後まで保留にする。確認日と任意のメモを、備考にも残す。"""
    if not 1 <= business_days <= HOLD_MAX_DAYS:
        raise ValueError("保留の営業日数は、1〜10の範囲で指定してください")
    until = add_business_days(today, business_days)
    c.hold_until = until
    memo = (memo or "").strip()
    _append_note(c, f"保留 {today:%Y-%m-%d}（確認日 {until:%Y-%m-%d}）" + (f"：{memo}" if memo else ""))
    return until


def release_hold(c: Case) -> None:
    """保留を解除して、警告の対象に戻す。"""
    c.hold_until = None


def record_cancel(c: Case, cancel_date: date, reason: str = "") -> None:
    """案件を取りやめにする（退職をやめたなど）。日付と理由は、備考にも残す。"""
    c.cancel_date = cancel_date
    reason = (reason or "").strip()
    _append_note(c, f"取りやめ {cancel_date:%Y-%m-%d}" + (f"：{reason}" if reason else ""))


def restore_cancel(c: Case, today: date) -> None:
    """取りやめを取り消して、元の状態に戻す。取り消したことは、備考に履歴として残す。"""
    if c.cancel_date:
        c.cancel_date = None
        _append_note(c, f"取りやめを取り消し {today:%Y-%m-%d}")


def apply_mail(s: Session, norm: CompanyNormalizer, kind: str, case_no: str, company_raw: str,
               recv: date, res: SyncResult) -> None:
    c = s.get(Case, case_no)
    if kind == KIND_CREATE:
        cr = norm.normalize(company_raw) if company_raw else None
        if c is None:
            c = Case(case_no=case_no, company=cr.name if cr else "", received_date=recv, note="")
            for n in (cr.notes if cr else []):
                _append_note(c, n)
            s.add(c)
            res.new_cases += 1
        else:
            # 処理完了メールが先に来て仮登録されていた場合の補完
            if c.received_date is None:
                c.received_date = recv
            if not c.company and cr:
                c.company = cr.name
                for n in cr.notes:
                    _append_note(c, n)
            if c.note and NOTE_UNSEEN in c.note:
                c.note = c.note.replace(NOTE_UNSEEN, "").strip("　 ")
    elif kind == KIND_DONE:
        if c is None:
            c = Case(case_no=case_no, company="", received_date=None, done_date=recv, note=NOTE_UNSEEN)
            s.add(c)
            res.done_cases += 1
        elif c.done_date is None:
            c.done_date = recv
            if c.cancel_date:  # 取りやめにしたあとで処理完了メールが届いた場合は、処理済を優先し、備考に残す
                _append_note(c, NOTE_DONE_AFTER_CANCEL)
            res.done_cases += 1
    s.flush()


def import_mails(s: Session, st: Settings, source: MailSource, norm: CompanyNormalizer,
                 res: SyncResult, interactive: bool = False) -> None:
    folder_id = source.find_folder_id(st.folder_name, interactive)
    last = db.get_meta(s, "last_sync")
    if last:
        since = datetime.fromisoformat(last).astimezone() - timedelta(days=1)
    else:
        since = datetime.now().astimezone() - timedelta(days=max(1, st.initial_days))
    started = datetime.now().astimezone()
    messages = source.fetch_messages(folder_id, since, interactive)
    messages.sort(key=lambda m: m.received)
    for m in messages:
        if s.get(ProcessedMail, m.mail_id):
            continue
        kind = classify(m.subject, st.kw_create, st.kw_done)
        if kind is None:
            continue
        body = html_to_text(m.body) if m.body_is_html else m.body
        parsed = parse_body(kind, body)
        if parsed is None:
            res.errors.append(f"案件番号を読み取れないメール: {m.subject[:40]}")
            log.warning("案件番号なし: %s", m.subject)
            s.add(ProcessedMail(mail_id=m.mail_id, kind=kind, case_no=""))
            continue
        apply_mail(s, norm, kind, parsed.case_no, parsed.company_raw, m.received_date, res)
        s.add(ProcessedMail(mail_id=m.mail_id, kind=kind, case_no=parsed.case_no))
        log.info("取り込み: %s %s", kind, parsed.case_no)
    db.set_meta(s, "last_sync", started.isoformat())
    s.commit()


# ---------- 保存期間 ----------
def purge_old_done(s: Session, today: date) -> int:
    """処理済日、または取りやめ日から1年（365日）をすぎた案件を削除する。未完了の案件は消さない。

    処理済日があるときは、処理済日で判断する（取りやめのあとに処理済になった案件は、処理済日から数える）。
    """
    cutoff = today - timedelta(days=RETENTION_DAYS)
    nos = list(s.scalars(select(Case.case_no).where(or_(
        and_(Case.done_date.is_not(None), Case.done_date < cutoff),
        and_(Case.done_date.is_(None), Case.cancel_date.is_not(None), Case.cancel_date < cutoff)))))
    if nos:
        s.execute(delete(NotifyLog).where(NotifyLog.case_no.in_(nos)))
        s.execute(delete(Case).where(Case.case_no.in_(nos)))
        s.commit()
        log.info("保存期間（%d日）をすぎた処理済の案件を削除: %d件", RETENTION_DAYS, len(nos))
    return len(nos)


# ---------- 通知 ----------
def build_notice(alerts: list[Alert], st: Settings) -> tuple[str, str]:
    lines = [f"特退金脱退案件で、期限を過ぎた案件が{len(alerts)}件あります。", ""]
    for kind, title, days in (
        (ALERT_DOCS, "【書類未着】案件作成から", st.warn_docs_days),
        (ALERT_DEFECT, "【補正書類未着】不備連絡から", st.warn_defect_days),
        (ALERT_PROCESS, "【WEB処理未完了】書類到着から", st.warn_process_days),
    ):
        group = [a for a in alerts if a.kind == kind]
        if not group:
            continue
        lines.append(f"{title}{days}営業日以上")
        for a in group:
            lines.append(f"・{a.case_no}　{a.company or '（会社名なし）'}　基準日 {a.base_date:%Y-%m-%d}　経過 {a.days}営業日")
        lines.append("")
    lines.append("このメールは特退金脱退 処理漏れ防止アプリから自動送信されています。")
    return f"【特退金脱退】処理漏れ警告 {len(alerts)}件", "\n".join(lines)


def notify_managers(s: Session, st: Settings, sender, today: date, res: SyncResult) -> None:
    if not st.notify_enabled:
        return
    to = st.managers()
    if not to:
        return
    cases = list(s.scalars(select(Case)))
    alerts = collect_alerts(cases, today, st)
    pending = []
    for a in alerts:
        done = s.scalar(select(NotifyLog).where(
            NotifyLog.case_no == a.case_no, NotifyLog.kind == a.kind, NotifyLog.notified_on == today))
        if done is None:
            pending.append(a)
    if not pending:
        return
    subject, body = build_notice(pending, st)
    sender.send_mail(to, subject, body, False)
    for a in pending:
        s.add(NotifyLog(case_no=a.case_no, kind=a.kind, notified_on=today))
    s.commit()
    res.notified += len(pending)
    log.info("管理職へ通知: %d件 -> %s", len(pending), ",".join(to))


# ---------- 保険会社への催促（書類未着） ----------
KIND_REMIND_SENT = "ins_sent"  # 送信済。通知ログの種類は「ins_sent_<何営業日目>」の形で、区切りごとに記録する
KIND_REMIND_SKIP = "ins_skip"  # 「今回は送らない」を選んだもの


@dataclass(frozen=True)
class Reminder:
    case_no: str
    company: str
    received_date: date
    days: int  # 案件作成日からの経過営業日
    milestone: int  # 何営業日目の区切りか（間隔が7なら 7, 14, 21…）


def _remind_kinds(milestone: int) -> list[str]:
    return [f"{KIND_REMIND_SENT}_{milestone}", f"{KIND_REMIND_SKIP}_{milestone}"]


def collect_reminders(s: Session, st: Settings, today: date) -> list[Reminder]:
    """催促メールの候補を返す。書類待ちの案件が、案件作成日から間隔（営業日）の区切りに達していて、
    その区切りで、まだ送信も「送らない」も記録していないもの。宛先が未登録なら空。"""
    if not st.insurer_emails():
        return []
    n = max(1, st.remind_days)
    out: list[Reminder] = []
    for c in s.scalars(select(Case)):
        if c.status != db.STATUS_RECEIVED or c.received_date is None or is_on_hold(c, today):
            continue
        days = business_days_between(c.received_date, today)
        if days < n:
            continue
        milestone = days // n * n  # 長く止まっていても、直近の区切りの1通だけ
        handled = s.scalar(select(NotifyLog).where(
            NotifyLog.case_no == c.case_no, NotifyLog.kind.in_(_remind_kinds(milestone))))
        if handled is None:
            out.append(Reminder(c.case_no, c.company, c.received_date, days, milestone))
    return sorted(out, key=lambda r: (r.received_date, r.case_no))


def build_reminder_mail(reminders: list[Reminder], st: Settings) -> tuple[str, str]:
    """件名と本文を作る。冒頭・末尾・件名は設定の定型文、案件の一覧は自動。"""
    lines = []
    if st.remind_intro.strip():
        lines += [st.remind_intro.strip(), ""]
    for r in reminders:
        lines.append(f"・{r.case_no}　{r.company or '（会社名なし）'}　案件作成日 {r.received_date:%Y-%m-%d}　経過 {r.days}営業日")
    if st.remind_outro.strip():
        lines += ["", st.remind_outro.strip()]
    subject = st.remind_subject.replace("{count}", str(len(reminders))).strip() or "書類未着のご確認"
    return subject, "\n".join(lines)


def send_reminders(s: Session, st: Settings, sender, send: list[Reminder], skip: list[Reminder],
                   today: date) -> None:
    """選んだ案件の催促を1通にまとめて送り、送信済と「送らない」を記録する。
    送信に失敗したときは例外のまま何も記録しない（次の機会に、もう一度確認する）。"""
    if send:
        subject, body = build_reminder_mail(send, st)
        sender.send_mail(st.insurer_emails(), subject, body, False)
        for r in send:
            s.add(NotifyLog(case_no=r.case_no, kind=_remind_kinds(r.milestone)[0], notified_on=today))
        log.info("保険会社へ催促: %d件 -> %s", len(send), ",".join(st.insurer_emails()))
    for r in skip:
        s.add(NotifyLog(case_no=r.case_no, kind=_remind_kinds(r.milestone)[1], notified_on=today))
    s.commit()


def run_sync(st: Settings, client, norm: CompanyNormalizer, interactive: bool = False,
             today: date | None = None) -> SyncResult:
    """メール取り込み → 警告判定 → 管理職への通知。失敗は errors に集めて続行する。"""
    res = SyncResult()
    today = today or date.today()
    try:  # 取り込みや古い案件の削除より前に、その日の最初のバックアップを取る
        db.daily_backup(today=today)
    except Exception:  # noqa: BLE001
        log.exception("バックアップに失敗")
    with db.session() as s:
        try:
            import_mails(s, st, client, norm, res, interactive)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            res.auth_required |= isinstance(e, GraphAuthRequired)
            res.errors.append(f"メール取り込みに失敗: {e}")
            log.exception("メール取り込み失敗")
        try:
            purge_old_done(s, today)
        except Exception:  # noqa: BLE001
            s.rollback()
            log.exception("古い処理済案件の削除に失敗")
        try:
            notify_managers(s, st, client, today, res)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            res.auth_required |= isinstance(e, GraphAuthRequired)
            res.errors.append(f"管理職への通知に失敗: {e}")
            log.exception("通知失敗")
        db.set_meta(s, "last_run", datetime.now().isoformat(timespec="seconds"))
        s.commit()
    return res
