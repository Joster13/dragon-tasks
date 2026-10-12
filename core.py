"""Persistent tasks and explicit Russian due-date parsing; no external services."""
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LISTS = {"работа": "Работа", "личное": "Личное", "учёба": "Учёба", "учеба": "Учёба", "vpn": "VPN"}


def parse_task(text, now):
    """Accept title [#list] [| deadline]. Never silently discard an invalid date."""
    parts = text.strip().rsplit("|", 1)
    title = parts[0].strip()
    category = "Входящие"
    for tag in re.findall(r"#([\wёЁ]+)", title):
        if tag.lower() in LISTS:
            category = LISTS[tag.lower()]
            title = re.sub(r"#" + re.escape(tag) + r"\b", "", title).strip()
    if not title or len(title) > 1500:
        raise ValueError("Текст задачи должен содержать от 1 до 1500 символов.")
    due = None
    if len(parts) == 2:
        due = parse_due(parts[1].strip(), now)
    else:
        # Extract only explicit date/time phrases, avoiding random numbers in titles.
        clock = r"(?:\d{1,2}:\d{2}|(?:\d{1,2}|одиннадцать|двенадцать|десять|девять|восемь|семь|шесть|пять|четыре|три|два|один)(?:\s+час(?:а|ов)?)?)"
        patterns = [r"через\s+\d{1,4}\s*(?:минут[уы]?|мин|час(?:а|ов)?|день|дня|дней)",
                    rf"(?:сегодня|завтра|послезавтра)\s+(?:в\s+)?{clock}",
                    rf"(?:в\s+)?(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\s+(?:в\s+)?{clock}",
                    r"\d{2}\.\d{2}(?:\.\d{4})?\s+(?:в\s+)?\d{1,2}:\d{2}"]
        matches = [m for p in patterns for m in re.finditer(p,title,re.I)]
        if len(matches)>1:
            raise ValueError("Нашёл несколько сроков. Укажи один срок после | или назначь его кнопкой.")
        if matches:
            m = matches[0]
            due = parse_due(m.group(),now)
            title = (title[:m.start()]+title[m.end():]).strip(" ,.!-")
            title = re.sub(r"^напомни(?:ть)?(?:\s+мне)?\s*", "", title, flags=re.I).strip(" ,.!-")
            if not title:
                raise ValueError("Добавь, о чём напомнить.")
    return title, category, due


def parse_due(text, now):
    s = text.lower().strip().replace("ё", "е")
    relative = re.fullmatch(r"через\s+(\d{1,4})\s*(мин(?:ут[уы]?)?|м|час(?:а|ов)?|ч|д(?:ень|ня|ней)?|д)", s)
    if relative:
        n, unit = relative.groups()
        n = int(n)
        if n < 1:
            raise ValueError("Интервал должен быть больше нуля.")
        delta = timedelta(minutes=n) if unit.startswith("м") else timedelta(hours=n) if unit.startswith("ч") else timedelta(days=n)
        return int((now + delta).timestamp())
    # Spoken hours and weekdays are normalized before strict parsing.
    words = {"один":1,"два":2,"три":3,"четыре":4,"пять":5,"шесть":6,"семь":7,"восемь":8,"девять":9,"десять":10,"одиннадцать":11,"двенадцать":12}
    for word, number in words.items():
        s = re.sub(r"\b"+word+r"\b",str(number),s)
    s = re.sub(r"(?:\s+час(?:а|ов)?)$","",s)
    s = re.sub(r"(?<![:\d])(\d{1,2})$",r"\1:00",s)
    weekdays = {"понедельник":0,"вторник":1,"среду":2,"четверг":3,"пятницу":4,"субботу":5,"воскресенье":6}
    week = re.fullmatch(r"(?:в\s+)?("+"|".join(weekdays)+r")\s+(?:в\s+)?(\d{1,2}):(\d{2})",s)
    if week:
        day,hour,minute = week.groups()
        try:
            target = now.replace(hour=int(hour),minute=int(minute),second=0,microsecond=0)+timedelta(days=(weekdays[day]-now.weekday())%7)
        except ValueError as exc:
            raise ValueError("Такого времени нет.") from exc
        if target<=now:
            target += timedelta(days=7)
        return int(target.timestamp())
    match = re.fullmatch(r"(?:(сегодня|завтра|послезавтра|\d{2}\.\d{2}(?:\.\d{4})?)\s+)?(?:в\s+)?(\d{1,2}):(\d{2})", s)
    if not match:
        raise ValueError("Не понял срок. Пример: завтра 18:00, 15.10.2026 09:30 или через 30 минут.")
    day, hour, minute = match.groups()
    try:
        target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if day in ("сегодня", "завтра", "послезавтра"):
            target += timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[day])
        elif day:
            bits = day.split(".")
            target = target.replace(day=int(bits[0]), month=int(bits[1]), year=int(bits[2]) if len(bits) == 3 else now.year)
            if len(bits) == 2 and target <= now:
                target = target.replace(year=now.year + 1)
        elif target <= now:
            target += timedelta(days=1)
    except ValueError as exc:
        raise ValueError("Такой даты или времени нет.") from exc
    if target <= now:
        raise ValueError("Срок уже прошёл. Укажи будущую дату.")
    return int(target.timestamp())


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
          category TEXT NOT NULL, due INTEGER, done INTEGER NOT NULL DEFAULT 0,
          voice TEXT, created INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
          retry_at INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(user_id,done,id);
        CREATE INDEX IF NOT EXISTS tasks_due ON tasks(done,notified,due);
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, digest TEXT NOT NULL DEFAULT '09:00',
          last_digest TEXT, retry_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS attachments(
          id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,
          kind TEXT NOT NULL, file_id TEXT NOT NULL, source_id INTEGER,
          UNIQUE(task_id,source_id));
        CREATE TABLE IF NOT EXISTS groups(
          user_id INTEGER NOT NULL, group_id TEXT NOT NULL, task_id INTEGER NOT NULL,
          PRIMARY KEY(user_id,group_id));
        CREATE TABLE IF NOT EXISTS inputs(
          user_id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, action TEXT NOT NULL);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if "draft" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN draft INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    def register(self, uid):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))

    def add(self, uid, title, category, due, created, voice=None):
        with self.db:
            return self.db.execute("INSERT INTO tasks(user_id,title,category,due,created,voice) VALUES(?,?,?,?,?,?)", (uid,title,category,due,created,voice)).lastrowid

    def get(self, uid, tid):
        return self.db.execute("SELECT * FROM tasks WHERE user_id=? AND id=?", (uid,tid)).fetchone()

    def list(self, uid, mode="all", category=None, now=None, limit=12, offset=0):
        where, args = "user_id=? AND done=? AND draft=?", [uid, int(mode == "done"),int(mode == "drafts")]
        if category:
            where += " AND category=?"
            args.append(category)
        if mode == "today":
            start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            end = int((now.replace(hour=0, minute=0, second=0, microsecond=0)+timedelta(days=1)).timestamp())
            where += " AND due>=? AND due<?"
            args.extend([start,end])
        elif mode == "overdue":
            where += " AND due<?"
            args.append(int(now.timestamp()))
        return self.db.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY due IS NULL,due,id LIMIT ? OFFSET ?", (*args,limit,offset)).fetchall()

    def finish(self, uid, tid):
        with self.db:
            return self.db.execute("UPDATE tasks SET done=1 WHERE user_id=? AND id=? AND done=0", (uid,tid)).rowcount

    def snooze(self, uid, tid, due):
        with self.db:
            return self.db.execute("UPDATE tasks SET due=?,notified=0,retry_at=0 WHERE user_id=? AND id=? AND done=0", (due,uid,tid)).rowcount

    def delete(self, uid, tid):
        with self.db:
            if not self.get(uid,tid):
                return 0
            self.db.execute("DELETE FROM attachments WHERE task_id=?",(tid,))
            self.db.execute("DELETE FROM groups WHERE task_id=?",(tid,))
            self.db.execute("DELETE FROM inputs WHERE task_id=?",(tid,))
            return self.db.execute("DELETE FROM tasks WHERE user_id=? AND id=?", (uid,tid)).rowcount

    def pending(self, ts):
        return self.db.execute("SELECT * FROM tasks WHERE draft=0 AND done=0 AND notified=0 AND due<=? AND retry_at<=? LIMIT 50", (ts,ts)).fetchall()

    def sent(self, tid):
        with self.db:
            self.db.execute("UPDATE tasks SET notified=1 WHERE id=?", (tid,))

    def retry(self, tid, ts):
        with self.db:
            self.db.execute("UPDATE tasks SET retry_at=? WHERE id=?", (ts,tid))

    def digest_users(self):
        return self.db.execute("SELECT * FROM users WHERE digest!='off'").fetchall()

    def set_digest(self, uid, value):
        self.register(uid)
        with self.db:
            self.db.execute("UPDATE users SET digest=?,retry_at=0 WHERE id=?", (value,uid))

    def digest_sent(self, uid, day):
        with self.db:
            self.db.execute("UPDATE users SET last_digest=?,retry_at=0 WHERE id=?", (day,uid))

    def digest_retry(self, uid, ts):
        with self.db:
            self.db.execute("UPDATE users SET retry_at=? WHERE id=?", (ts,uid))

    def export(self, uid):
        result = []
        for r in self.db.execute("SELECT * FROM tasks WHERE user_id=? ORDER BY id", (uid,)):
            item = dict(r)
            item["attachments"] = [dict(a) for a in self.attachments(r["id"])]
            result.append(item)
        return result

    def edit(self, uid, tid, **fields):
        if not fields or not set(fields)<= {"title","category","draft"}:
            raise ValueError("Invalid fields")
        with self.db:
            return self.db.execute("UPDATE tasks SET "+",".join(k+"=?" for k in fields)+" WHERE user_id=? AND id=?",(*fields.values(),uid,tid)).rowcount

    def attach(self, uid, tid, kind, file_id, source_id):
        if not self.get(uid,tid):
            return False
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO attachments(task_id,kind,file_id,source_id) VALUES(?,?,?,?)",(tid,kind,file_id,source_id))
        return True

    def attachments(self, tid):
        return self.db.execute("SELECT * FROM attachments WHERE task_id=? ORDER BY id",(tid,)).fetchall()

    def group(self, uid, gid, tid=None):
        if tid:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO groups VALUES(?,?,?)",(uid,gid,tid))
        row = self.db.execute("SELECT task_id FROM groups WHERE user_id=? AND group_id=?",(uid,gid)).fetchone()
        return row[0] if row else None

    def input(self, uid, tid=None, action=None):
        if tid is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO inputs VALUES(?,?,?)",(uid,tid,action))
        return self.db.execute("SELECT * FROM inputs WHERE user_id=?",(uid,)).fetchone()

    def clear_input(self, uid):
        with self.db:
            self.db.execute("DELETE FROM inputs WHERE user_id=?",(uid,))

    def close(self):
        self.db.close()
