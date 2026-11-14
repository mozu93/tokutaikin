import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokutai import db, paths, service  # noqa: E402
from tokutai.bizdays import business_days_between  # noqa: E402
from tokutai.corporate import CompanyNormalizer, NOTE_AMBIGUOUS, NOTE_UNKNOWN, load_table  # noqa: E402
from tokutai.graph import GraphAuthRequired, GraphError, MailItem  # noqa: E402
from tokutai.mailparse import KIND_CREATE, KIND_DONE, classify, parse_body  # noqa: E402
from tokutai.settings import Settings  # noqa: E402


@pytest.fixture(scope="module")
def norm():
    return CompanyNormalizer(load_table(paths.bundled_abbr_path()))


@pytest.mark.parametrize("raw,expected", [
    ("ｻﾝﾌﾟﾙ (ｶ", "サンプル株式会社"),
    ("ｶ)ｻﾝﾌﾟﾙ", "株式会社サンプル"),
    ("ﾏﾙﾏﾙ(ｶ)ｴｲｷﾞﾖｳ", "マルマル株式会社エイギヨウ"),
    ("ｱｲｳ(ﾕ", "アイウ有限会社"),
    ("ｼﾞｮｳﾎﾞｳ(ｼﾎｳ", "ジョウボウ司法書士法人"),
    ("ﾃｽﾄ(ｼ", "テスト合資会社"),
])
def test_company(norm, raw, expected):
    assert norm.normalize(raw).name == expected


def test_company_notes(norm):
    assert NOTE_AMBIGUOUS in norm.normalize("ｱｲｳ(ｲ").notes
    assert NOTE_UNKNOWN in norm.normalize("ｱｲｳ(ﾎﾎﾎ").notes
    assert norm.normalize("").name == ""


def test_classify_and_parse():
    assert classify("特退金 脱退案件作成のお知らせ", "特退金脱退案件作成", "特退金制度申し込み") == KIND_CREATE
    assert classify("〇〇生命：特退金制度申し込み", "特退金脱退案件作成", "特退金制度申し込み") == KIND_DONE
    assert classify("関係ないメール", "特退金脱退案件作成", "特退金制度申し込み") is None
    p = parse_body(KIND_CREATE, "案件番号：T900000000000001\n上記書類を ｻﾝﾌﾟﾙ (ｶ 様へ送付")
    assert p.case_no == "T900000000000001" and p.company_raw == "ｻﾝﾌﾟﾙ (ｶ"
    assert parse_body(KIND_DONE, "本文に番号なし") is None


def test_bizdays():
    # 2026-11-13(金) -> 11-16(月): 1営業日。11-23(月,勤労感謝の日)は除外
    assert business_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 1
    assert business_days_between(date(2026, 11, 20), date(2026, 11, 24)) == 1
    assert business_days_between(date(2026, 11, 20), date(2026, 11, 20)) == 0


class FakeClient:
    def __init__(self, msgs):
        self.msgs, self.sent = msgs, []

    def find_folder_id(self, name, interactive=False):
        return "f1"

    def fetch_messages(self, folder_id, since, interactive=False):
        return list(self.msgs)

    def send_mail(self, to, subject, body, interactive=False):
        self.sent.append((to, subject, body))


def mail(i, subj, body, d):
    return MailItem(i, subj, datetime(d.year, d.month, d.day, 10).astimezone(), body, False)


def test_sync_flow(tmp_path, monkeypatch, norm):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    st = Settings(folder_name="x", manager_emails=["a@example.com", "b@example.com"])
    msgs = [
        mail("m1", "特退金脱退案件作成", "案件番号：T1001\n上記書類を ｻﾝﾌﾟﾙ (ｶ 様へ", date(2026, 11, 2)),
        mail("m2", "特退金脱退案件作成", "案件番号：T1002\n上記書類を ﾃｽﾄ(ﾕ 様へ", date(2026, 11, 4)),
        mail("m3", "〇〇生命：特退金制度申し込み", "案件番号：T1002", date(2026, 11, 6)),
        mail("m4", "〇〇生命：特退金制度申し込み", "案件番号：T9999", date(2026, 11, 6)),
    ]
    client = FakeClient(msgs)
    r = service.run_sync(st, client, norm, today=date(2026, 11, 12))
    assert (r.new_cases, r.done_cases, r.errors) == (2, 2, [])
    with db.session() as s:
        c1, c2, c9 = s.get(db.Case, "T1001"), s.get(db.Case, "T1002"), s.get(db.Case, "T9999")
        assert c1.company == "サンプル株式会社" and c1.status == "書類待ち"
        assert c2.status == "処理済" and c2.note == ""  # 書類到着を記録せず処理済になっても、備考は付けない
        assert c9.note == service.NOTE_UNSEEN
        assert c1.note == ""
        assert service.alert_kind(c1, date(2026, 11, 12), st) == service.ALERT_DOCS  # 7営業日
        assert service.alert_kind(c1, date(2026, 11, 10), st) is None
    assert len(client.sent) == 1 and client.sent[0][0] == ["a@example.com", "b@example.com"]
    # 同日の再実行では二重送信しない・メールも二重取り込みしない
    r2 = service.run_sync(st, client, norm, today=date(2026, 11, 12))
    assert r2.new_cases == 0 and r2.notified == 0 and len(client.sent) == 1
    # 翌日は再通知
    service.run_sync(st, client, norm, today=date(2026, 11, 13))
    assert len(client.sent) == 2


class AuthLostClient(FakeClient):
    """サインインが切れた状態（保存済みトークンが使えない）を再現する。"""

    def __init__(self, exc):
        super().__init__([])
        self.exc = exc

    def find_folder_id(self, name, interactive=False):
        raise self.exc


def test_sync_auth_required(tmp_path, monkeypatch, norm):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    st = Settings(folder_name="x")
    r = service.run_sync(st, AuthLostClient(GraphAuthRequired("サインインが必要です。")), norm,
                         today=date(2026, 11, 12))
    assert r.auth_required and r.errors
    # 通信エラーなど、サインイン切れ以外ではフラグを立てない
    r2 = service.run_sync(st, AuthLostClient(GraphError("通信に失敗しました")), norm, today=date(2026, 11, 12))
    assert not r2.auth_required and r2.errors


def test_insurer_reminders(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    st = Settings(insurer_email="ins@example.com; ins2@example.com", remind_days=7)
    assert st.insurer_emails() == ["ins@example.com", "ins2@example.com"]
    with db.session() as s:
        s.add_all([
            db.Case(case_no="A", company="甲", received_date=date(2026, 11, 2)),  # 11/12で8営業日（書類待ち）
            db.Case(case_no="B", company="乙", received_date=date(2026, 11, 9)),  # まだ3営業日
            db.Case(case_no="C", company="丙", received_date=date(2026, 11, 2), docs_date=date(2026, 11, 5)),  # 書類到着
            db.Case(case_no="D", company="丁", received_date=date(2026, 11, 2), defect_date=date(2026, 11, 6)),  # 不備
        ])
        s.commit()
        today = date(2026, 11, 12)
        c = service.collect_reminders(s, st, today)
        assert [r.case_no for r in c] == ["A"] and c[0].milestone == 7
        # 宛先が未登録なら動かない
        assert service.collect_reminders(s, Settings(), today) == []
        # 送信すると、同じ区切りでは出ない。メールは1通にまとまる
        client = FakeClient([])
        service.send_reminders(s, st, client, c, [], today)
        assert len(client.sent) == 1 and client.sent[0][0] == ["ins@example.com", "ins2@example.com"]
        assert "A" in client.sent[0][2] and "甲" in client.sent[0][2]
        assert service.collect_reminders(s, st, date(2026, 11, 13)) == []
        # 次の区切り（14営業日目）で、あらためて候補になる
        assert business_days_between(date(2026, 11, 2), date(2026, 11, 24)) == 14  # 11/3・11/23は祝日
        later = {r.case_no: r.milestone for r in service.collect_reminders(s, st, date(2026, 11, 24))}
        assert later == {"A": 14, "B": 7}  # Bも7営業日目に達した


def test_reminder_mail_template():
    rs = [service.Reminder("A", "甲", date(2026, 11, 2), 8, 7), service.Reminder("B", "", date(2026, 11, 3), 7, 7)]
    subject, body = service.build_reminder_mail(rs, Settings())  # 既定の文面
    assert subject == "【特退金脱退】書類未着のご確認（2件）"
    assert body.startswith("お世話になっております。") and "・A　甲　案件作成日 2026-11-02　経過 8営業日" in body
    assert "（会社名なし）" in body
    # 設定で文面を直すと、件名・冒頭・末尾に反映され、案件の一覧は自動で入る
    st = Settings(remind_subject="催促 {count}件", remind_intro="冒頭です", remind_outro="署名です")
    subject, body = service.build_reminder_mail(rs[:1], st)
    assert subject == "催促 1件"
    lines = body.splitlines()
    assert lines[0] == "冒頭です" and lines[-1] == "署名です" and "・A　甲" in body
    # 冒頭・末尾が空でも、一覧は出る
    _, body = service.build_reminder_mail(rs[:1], Settings(remind_intro="", remind_outro=""))
    assert body.startswith("・A　甲")


def test_insurer_reminder_skip_and_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    st = Settings(insurer_email="ins@example.com")
    today = date(2026, 11, 12)
    with db.session() as s:
        s.add(db.Case(case_no="A", company="甲", received_date=date(2026, 11, 2)))
        s.commit()
        c = service.collect_reminders(s, st, today)

        class Failing(FakeClient):
            def send_mail(self, *a, **k):
                raise GraphError("送信失敗")

        with pytest.raises(GraphError):  # 送信に失敗したら、何も記録しない（次回また確認する）
            service.send_reminders(s, st, Failing([]), c, [], today)
        assert len(service.collect_reminders(s, st, today)) == 1
        # 「送らない」を選ぶと、メールは送らず、その区切りでは聞かない
        client = FakeClient([])
        service.send_reminders(s, st, client, [], c, today)
        assert client.sent == [] and service.collect_reminders(s, st, today) == []


def test_cancel_case(tmp_path, monkeypatch, norm):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    st = Settings(insurer_email="ins@example.com")
    today = date(2026, 11, 20)  # 案件作成日（11/2）から、書類未着の基準（7営業日）を超えている
    with db.session() as s:
        s.add_all([db.Case(case_no="A", company="甲", received_date=date(2026, 11, 2)),
                   db.Case(case_no="B", company="乙", received_date=date(2026, 11, 2))])
        s.commit()
        a = s.get(db.Case, "A")
        assert service.alert_kind(a, today, st) == service.ALERT_DOCS and service.elapsed_days(a, today) is not None
        assert [r.case_no for r in service.collect_reminders(s, st, today)] == ["A", "B"]

        # 取りやめにすると、警告・経過日数・催促の対象から外れる。日付と理由は備考に残る
        service.record_cancel(a, date(2026, 11, 14), "退職取りやめ")
        s.commit()
        assert a.status == db.STATUS_CANCELLED and a.note == "取りやめ 2026-11-14：退職取りやめ"
        assert service.alert_kind(a, today, st) is None and service.elapsed_days(a, today) is None
        assert [r.case_no for r in service.collect_reminders(s, st, today)] == ["B"]
        assert [x.case_no for x in service.collect_alerts(list(s.scalars(select_cases())), today, st)] == ["B"]

        # 一覧: 「未完了のみ」には出ない。「すべて」と検索には出る。標準の並びでは、最後
        cases = list(s.scalars(select_cases()))
        alerts = {x.case_no: x for x in service.collect_alerts(cases, today, st)}
        names = lambda mode, q="": [c.case_no for c in service.select_rows(cases, alerts, mode, q, "", True, today)]  # noqa: E731
        assert names(service.FILTER_OPEN) == ["B"] and names(service.FILTER_ALL) == ["B", "A"]
        assert names(service.FILTER_OPEN, "甲") == ["A"]
        assert [c.case_no for c in service.select_rows(cases, alerts, service.FILTER_ALL, "", "取りやめ日", True, today)] == ["A", "B"]

        # 取りやめ済には、到着・不備・処理済・取りやめを、重ねて記録しない
        sel = [("A", a.status), ("B", s.get(db.Case, "B").status)]
        for act in (service.BULK_ARRIVED, service.BULK_DEFECT, service.BULK_DONE, service.BULK_CANCEL):
            assert service.bulk_targets(sel, act) == (["B"], 1)

        # 取り消すと元の状態に戻り、取り消した履歴が備考に残る
        service.restore_cancel(a, date(2026, 11, 15))
        assert a.status == db.STATUS_RECEIVED and a.cancel_date is None
        assert a.note == "取りやめ 2026-11-14：退職取りやめ　取りやめを取り消し 2026-11-15"
        assert service.alert_kind(a, today, st) == service.ALERT_DOCS


def select_cases():
    from sqlalchemy import select
    return select(db.Case)


def test_cancel_undo_and_reason_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    with db.session() as s:
        s.add(db.Case(case_no="A", company="甲", received_date=date(2026, 11, 2), note="既存の備考"))
        s.commit()
        stack = service.UndoStack()
        service.apply_undoable(s, stack, "取りやめ", ["A"], lambda c: service.record_cancel(c, date(2026, 11, 14), ""))
        c = s.get(db.Case, "A")
        assert c.status == db.STATUS_CANCELLED and c.note == "既存の備考　取りやめ 2026-11-14"  # 理由は空でもよい
        label, restored, skipped = service.undo(s, stack)
        assert (label, restored, skipped) == ("取りやめ", 1, 0)
        c = s.get(db.Case, "A")
        assert c.cancel_date is None and c.note == "既存の備考"  # 日付も備考も元に戻る


def test_done_mail_after_cancel(tmp_path, monkeypatch, norm):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    with db.session() as s:
        s.add(db.Case(case_no="T1", company="甲", received_date=date(2026, 11, 2), docs_date=date(2026, 11, 4)))
        s.commit()
        service.record_cancel(s.get(db.Case, "T1"), date(2026, 11, 10), "取りやめ")
        s.commit()
        res = service.SyncResult()
        service.apply_mail(s, norm, KIND_DONE, "T1", "", date(2026, 11, 12), res)
        c = s.get(db.Case, "T1")
        assert c.status == db.STATUS_DONE  # 実際に処理されたので、処理済を優先する
        assert service.NOTE_DONE_AFTER_CANCEL in c.note and res.done_cases == 1


def test_purge_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    today = date(2026, 11, 20)
    with db.session() as s:
        s.add_all([
            db.Case(case_no="OLD", cancel_date=today - timedelta(days=366)),  # 取りやめから1年超 → 削除
            db.Case(case_no="NEW", cancel_date=today - timedelta(days=364)),  # 1年以内 → 残す
            db.Case(case_no="OPEN", received_date=date(2020, 1, 6)),  # 未完了は、古くても消さない
            db.Case(case_no="REDONE", cancel_date=today - timedelta(days=400),
                    done_date=today - timedelta(days=10)),  # 取りやめのあとに処理済 → 処理済日で判断し、残す
        ])
        s.commit()
        assert service.purge_old_done(s, today) == 1
        assert {c.case_no for c in s.scalars(select_cases())} == {"NEW", "OPEN", "REDONE"}


def test_csv_has_cancel_column(tmp_path):
    c = db.Case(case_no="A", company="甲", received_date=date(2026, 11, 2), cancel_date=date(2026, 11, 14), note="x")
    p = tmp_path / "o.csv"
    service.export_csv([c], p)
    rows = p.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].split(",")[-3:] == ["取りやめ日", "保留の確認日", "備考"]
    assert rows[1].split(",")[0] == "取りやめ" and "2026-11-14" in rows[1]


def test_company_sort_ignores_corporate_type(norm):
    """会社名の並び替えは、先頭・末尾の法人の種類（株式会社など）を無視する。"""
    w = norm.formal_words
    key = lambda n: service.company_sort_key(n, w)  # noqa: E731
    assert key("株式会社甲") == key("甲株式会社") == "甲"
    assert key("医療法人乙会") == "乙会" and key("社会福祉法人丙") == "丙"
    assert key("株式会社") == "株式会社"  # 除くと空になるときは、そのまま
    assert key("甲商事") == "甲商事"  # 途中にある語は、除かない
    cases = [db.Case(case_no=str(i), company=n) for i, n in enumerate(
        ["株式会社ウ", "ア株式会社", "有限会社イ", "合同会社エ", ""])]
    rows = service.select_rows(cases, {}, service.FILTER_ALL, "", "会社名", True, date(2026, 11, 20), w)
    assert [c.company for c in rows] == ["ア株式会社", "有限会社イ", "株式会社ウ", "合同会社エ", ""]  # 空は最後
    rows = service.select_rows(cases, {}, service.FILTER_ALL, "", "会社名", False, date(2026, 11, 20), w)
    assert [c.company for c in rows] == ["合同会社エ", "株式会社ウ", "有限会社イ", "ア株式会社", ""]


def test_hold_case(tmp_path, monkeypatch, norm):
    """保留: 確認日の前日まで警告・催促を止め、確認日に警告へ戻る。状態が変わると解く。"""
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    st = Settings(insurer_email="ins@example.com")
    today = date(2026, 11, 20)  # 金曜。案件作成日（11/2）から基準の7営業日を超えている
    with db.session() as s:
        s.add_all([db.Case(case_no="A", company="甲", received_date=date(2026, 11, 2), note="既存"),
                   db.Case(case_no="B", company="乙", received_date=date(2026, 11, 2))])
        s.commit()
        a = s.get(db.Case, "A")
        assert service.alert_kind(a, today, st) == service.ALERT_DOCS

        # 3営業日後（11/26木。11/23は祝日）まで保留。確認日とメモは備考に残る
        until = service.record_hold(a, today, 3, "26日に送ると連絡あり")
        s.commit()
        assert until == date(2026, 11, 26) and a.hold_until == until
        assert a.note == "既存　保留 2026-11-20（確認日 2026-11-26）：26日に送ると連絡あり"
        assert service.is_on_hold(a, today) and service.alert_kind(a, today, st) is None
        assert service.alert_kind(a, date(2026, 11, 25), st) is None  # 確認日の前日までは保留
        assert service.alert_kind(a, until, st) == service.ALERT_DOCS  # 確認日に警告へ戻る
        assert [r.case_no for r in service.collect_reminders(s, st, today)] == ["B"]  # 催促も止まる
        assert [x.case_no for x in service.collect_alerts(list(s.scalars(select_cases())), today, st)] == ["B"]

        # 一覧の「未完了のみ」には残る。処理済・取りやめ済は、保留にできない
        cases = list(s.scalars(select_cases()))
        rows = service.select_rows(cases, {}, service.FILTER_OPEN, "", "", True, today)
        assert {c.case_no for c in rows} == {"A", "B"}
        assert service.bulk_targets([("A", a.status), ("X", db.STATUS_DONE), ("Y", db.STATUS_CANCELLED)],
                                    service.BULK_HOLD) == (["A"], 2)

        # 解除すると、すぐ警告に戻る
        service.release_hold(a)
        assert not service.is_on_hold(a, today) and service.alert_kind(a, today, st) == service.ALERT_DOCS

        # 書類が届く・不備を連絡すると、保留は解ける（届いたあとの未処理の警告を、隠さない）
        service.record_hold(a, today, 10)
        service.record_arrived(a, today)
        assert a.hold_until is None and a.docs_date == today
        service.record_hold(a, today, 1)
        service.record_defect(a, today)
        assert a.hold_until is None

        # 範囲外の日数は受け付けない
        for bad in (0, 11):
            with pytest.raises(ValueError):
                service.record_hold(a, today, bad)


def test_hold_undo_and_migrate(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cases (case_no VARCHAR(40) PRIMARY KEY, company VARCHAR(200), received_date DATE, "
                "docs_date DATE, done_date DATE, defect_date DATE, cancel_date DATE, note TEXT)")
    con.execute("INSERT INTO cases VALUES ('OLD1', '旧会社', '2026-11-02', NULL, NULL, NULL, NULL, '旧データ')")
    con.commit()
    con.close()
    db.init(f"sqlite:///{path}")  # 保留の列がない古いDBでも、列が足され、既存のデータは残る
    with db.session() as s:
        assert s.get(db.Case, "OLD1").hold_until is None and s.get(db.Case, "OLD1").note == "旧データ"
        stack = service.UndoStack()
        service.apply_undoable(s, stack, "保留", ["OLD1"], lambda c: service.record_hold(c, date(2026, 11, 20), 3))
        assert s.get(db.Case, "OLD1").hold_until == date(2026, 11, 26)
        service.undo(s, stack)
        c = s.get(db.Case, "OLD1")
        assert c.hold_until is None and c.note == "旧データ"


def test_migrate_adds_cancel_column(tmp_path, monkeypatch):
    """取りやめ日の列がない古いDBを開くと、列が足され、既存のデータは、そのまま残る。"""
    import sqlite3
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cases (case_no VARCHAR(40) PRIMARY KEY, company VARCHAR(200), received_date DATE, "
                "docs_date DATE, done_date DATE, defect_date DATE, note TEXT)")
    con.execute("INSERT INTO cases VALUES ('OLD1', '旧会社', '2026-11-02', NULL, NULL, NULL, '旧データ')")
    con.commit()
    con.close()
    db.init(f"sqlite:///{path}")
    with db.session() as s:
        c = s.get(db.Case, "OLD1")
        assert c.company == "旧会社" and c.note == "旧データ" and c.cancel_date is None
        assert c.status == db.STATUS_RECEIVED
        c.cancel_date = date(2026, 11, 14)
        s.commit()
        assert s.get(db.Case, "OLD1").status == db.STATUS_CANCELLED


def test_process_alert():
    st = Settings()
    c = db.Case(case_no="T1", received_date=date(2026, 11, 2), docs_date=date(2026, 11, 9))
    assert service.alert_kind(c, date(2026, 11, 11), st) is None
    assert service.alert_kind(c, date(2026, 11, 12), st) == service.ALERT_PROCESS


def test_defect_alert():
    st = Settings()
    c = db.Case(case_no="T2", received_date=date(2026, 11, 2), defect_date=date(2026, 11, 2))
    assert c.status == db.STATUS_DEFECT
    assert service.alert_kind(c, date(2026, 11, 11), st) is None  # 6営業日（11/3は祝日）
    assert service.alert_kind(c, date(2026, 11, 12), st) == service.ALERT_DEFECT  # 7営業日
    st.warn_defect_days = 3
    assert service.alert_kind(c, date(2026, 11, 6), st) == service.ALERT_DEFECT
    # 補正書類が届いたら、WEB処理の警告に切り替わる
    c.defect_date, c.docs_date = None, date(2026, 11, 9)
    assert c.status == db.STATUS_ARRIVED
    assert service.alert_kind(c, date(2026, 11, 12), st) == service.ALERT_PROCESS


def test_migrate_old_db(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE cases (case_no VARCHAR(40) PRIMARY KEY, company VARCHAR(200), "
                "received_date DATE, docs_date DATE, done_date DATE, note TEXT)")
    con.execute("INSERT INTO cases (case_no, company, note) VALUES ('X1','a','')")
    con.commit(); con.close()
    db.init(f"sqlite:///{p}")
    with db.session() as s:
        assert s.get(db.Case, "X1").defect_date is None


def test_record_defect_keeps_original_date():
    c = db.Case(case_no="T3", received_date=date(2026, 11, 2), docs_date=date(2026, 11, 5), note="メモ")
    service.record_defect(c, date(2026, 11, 9))
    assert c.docs_date is None and c.defect_date == date(2026, 11, 9)
    assert "元の書類到着日 2026-11-05" in c.note and "メモ" in c.note


def test_purge_old_done(tmp_path):
    db.init(f"sqlite:///{tmp_path / 'p.db'}")
    today = date(2026, 11, 12)
    with db.session() as s:
        s.add_all([
            db.Case(case_no="OLD", done_date=date(2025, 11, 11)),     # 366日前: 削除
            db.Case(case_no="EDGE", done_date=date(2025, 11, 12)),    # ちょうど365日前: 残す
            db.Case(case_no="NEW", done_date=date(2026, 6, 1)),
            db.Case(case_no="OPEN", received_date=date(2024, 1, 5)),  # 未完了は古くても残す
        ])
        s.commit()
        assert service.purge_old_done(s, today) == 1
        from sqlalchemy import select
        left = {c.case_no for c in s.scalars(select(db.Case))}
    assert left == {"EDGE", "NEW", "OPEN"}


def _cases_for_view():
    return [
        db.Case(case_no="A1", company="あ商事", received_date=date(2026, 11, 2), note="元の書類到着日 2026-11-05"),
        db.Case(case_no="A2", company="い工業", received_date=date(2026, 11, 4), defect_date=date(2026, 11, 6)),
        db.Case(case_no="A3", company="う運輸", received_date=date(2026, 10, 1), done_date=date(2026, 10, 9)),
        db.Case(case_no="A4", company="", received_date=None, docs_date=date(2026, 11, 9)),
    ]


def _nos(rows):
    return [c.case_no for c in rows]


def test_select_filters():
    cs, today = _cases_for_view(), date(2026, 11, 12)
    sel = lambda mode, q="", col="", asc=True: _nos(service.select_rows(cs, {}, mode, q, col, asc, today))
    assert set(sel("未完了のみ")) == {"A1", "A2", "A4"}
    assert set(sel("すべて")) == {"A1", "A2", "A3", "A4"}
    assert sel("不備のみ") == ["A2"]
    # 検索は備考も対象。検索中は「未完了のみ」でも処理済を探せる
    assert sel("未完了のみ", "元の書類") == ["A1"]
    assert sel("未完了のみ", "う運輸") == ["A3"]


def test_select_sort_puts_empty_last():
    cs, today = _cases_for_view(), date(2026, 11, 12)
    sel = lambda col, asc: _nos(service.select_rows(cs, {}, "すべて", "", col, asc, today))
    assert sel("案件作成日", True) == ["A3", "A1", "A2", "A4"]   # 空のA4は最後
    assert sel("案件作成日", False) == ["A2", "A1", "A3", "A4"]  # 降順でも空は最後
    assert sel("案件番号", False) == ["A4", "A3", "A2", "A1"]
    assert sel("状態", True) == ["A1", "A2", "A4", "A3"]     # 書類待ち → 不備 → 書類到着 → 処理済


def test_export_csv(tmp_path):
    cs = _cases_for_view()
    p = tmp_path / "out.csv"
    assert service.export_csv(cs, p) == 4
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel用のBOM
    import csv
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    assert rows[0] == service.CSV_HEADER
    assert [r[1] for r in rows[1:]] == ["A4", "A3", "A1", "A2"]  # 案件作成日の古い順、空が先頭
    a3 = next(r for r in rows if r[1] == "A3")
    assert a3[0] == "処理済" and a3[6] == "2026-10-09"


def test_bulk_targets():
    sel = [("A", db.STATUS_RECEIVED), ("B", db.STATUS_DEFECT), ("C", db.STATUS_ARRIVED), ("D", db.STATUS_DONE)]
    assert service.bulk_targets(sel, service.BULK_ARRIVED) == (["A", "B", "C"], 1)
    assert service.bulk_targets(sel, service.BULK_DEFECT) == (["A", "C"], 2)   # 不備中・処理済は対象外
    assert service.bulk_targets(sel, service.BULK_DONE) == (["A", "B", "C"], 1)
    assert service.bulk_targets([("D", db.STATUS_DONE)], service.BULK_ARRIVED) == ([], 1)


def test_undo_multi_step(tmp_path):
    db.init(f"sqlite:///{tmp_path / 'u.db'}")
    stack = service.UndoStack()
    with db.session() as s:
        s.add(db.Case(case_no="U1", received_date=date(2026, 11, 2), docs_date=date(2026, 11, 5)))
        s.add(db.Case(case_no="U2", received_date=date(2026, 11, 2)))
        s.commit()
        # 操作1: 不備を連絡（書類到着日が消え、備考が付く）
        service.apply_undoable(s, stack, "不備連絡", ["U1"], lambda c: service.record_defect(c, date(2026, 11, 9)))
        # 操作2: 2件まとめて処理済
        service.apply_undoable(s, stack, "処理済", ["U1", "U2"], lambda c: setattr(c, "done_date", date(2026, 11, 10)))
        # 変化のない操作は履歴に残さない
        assert service.apply_undoable(s, stack, "何もしない", ["U2"], lambda c: None) == 0
        assert len(stack) == 2 and stack.peek_label() == "処理済"

        assert service.undo(s, stack) == ("処理済", 2, 0)
        u1 = s.get(db.Case, "U1")
        assert u1.done_date is None and u1.defect_date == date(2026, 11, 9)
        assert service.undo(s, stack) == ("不備連絡", 1, 0)
        s.refresh(u1)
        assert u1.defect_date is None and u1.docs_date == date(2026, 11, 5) and u1.note == ""  # 元通り
        assert service.undo(s, stack) is None


def test_undo_skips_changed_and_removes_created(tmp_path):
    db.init(f"sqlite:///{tmp_path / 'u2.db'}")
    stack = service.UndoStack(limit=2)
    with db.session() as s:
        s.add(db.Case(case_no="V1", received_date=date(2026, 11, 2)))
        s.commit()
        service.apply_undoable(s, stack, "到着", ["V1"], lambda c: setattr(c, "docs_date", date(2026, 11, 5)))
        s.get(db.Case, "V1").done_date = date(2026, 11, 6)  # その後にメール取り込みなどで変わった
        s.commit()
        assert service.undo(s, stack) == ("到着", 0, 1)     # 上書きせず、戻さない
        assert s.get(db.Case, "V1").docs_date == date(2026, 11, 5)
        # 追加した案件は、削除して戻す
        c = db.Case(case_no="V2", received_date=date(2026, 11, 2))
        s.add(c); s.commit()
        stack.push("追加", {"V2": (None, service.snapshot(c))})
        assert service.undo(s, stack) == ("追加", 1, 0) and s.get(db.Case, "V2") is None
    # 履歴は上限まで
    for i in range(5):
        stack.push(str(i), {"X": ({}, {"a": i})})
    assert len(stack) == 2


def test_daily_backup(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 'live.db'}")
    with db.session() as s:
        s.add(db.Case(case_no="B1", company="バックアップ確認"))
        s.commit()
    assert db.daily_backup(keep=2, today=date(2026, 11, 10)) is True
    p = tmp_path / "backup" / "tokutai_20261110.db"
    with closing(sqlite3.connect(p)) as c:  # Windowsでは開いたままだと古いバックアップを消せない
        assert [r[0] for r in c.execute("select case_no from cases")] == ["B1"]
    assert db.daily_backup(keep=2, today=date(2026, 11, 10)) is False  # 同じ日は作り直さない
    db.daily_backup(keep=2, today=date(2026, 11, 11))
    db.daily_backup(keep=2, today=date(2026, 11, 12))
    names = sorted(x.name for x in (tmp_path / "backup").glob("*"))
    assert names == ["tokutai_20261111.db", "tokutai_20261112.db"]  # 古いものから消える


def test_run_sync_makes_backup(tmp_path, monkeypatch, norm):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 'live2.db'}")
    st = Settings(folder_name="x")
    service.run_sync(st, FakeClient([]), norm, today=date(2026, 11, 12))
    assert (tmp_path / "backup" / "tokutai_20261112.db").exists()
