"""Persistent tasks and explicit Russian due-date parsing; no external services."""
import re
import calendar
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LISTS = {"работа": "Работа", "личное": "Личное", "учёба": "Учёба", "учеба": "Учёба", "vpn": "VPN"}


MONTHS = {name: number for number, names in enumerate([
    ('января','январь'),('февраля','февраль'),('марта','март'),('апреля','апрель'),
    ('мая','май'),('июня','июнь'),('июля','июль'),('августа','август'),
    ('сентября','сентябрь'),('октября','октябрь'),('ноября','ноябрь'),('декабря','декабрь')],1) for name in names}
WEEKDAYS = {'понедельник':0,'вторник':1,'среду':2,'среда':2,'четверг':3,'пятницу':4,'пятница':4,'субботу':5,'суббота':5,'воскресенье':6}


def parse_task(text, now):
    """Accept title [#list] [/ deadline]. Never silently discard an invalid date."""
    parts = re.split(r"/\s+|\s*\|\s*", text.strip(), maxsplit=1)
    title = parts[0].strip()
    category = "Личное"
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
        optional_time = rf"(?:\s+(?:в\s+)?{clock})?"
        patterns = [r"\bчерез\s+\d{1,4}\s*(?:минут[уы]?|мин|час(?:а|ов)?|день|дня|дней)\b",
                    rf"\b(?:сегодня|завтра|послезавтра)\b{optional_time}",
                    rf"\b(?:в\s+)?(?:(?:этот|эту|это|следующий|следующую|следующее)\s+)?(?:{'|'.join(WEEKDAYS)})\b{optional_time}",
                    rf"\b\d{{1,2}}\s+(?:{'|'.join(MONTHS)})\b(?:\s+\d{{4}}(?:\s*(?:года|г\.?))?)?{optional_time}",
                    rf"(?<![\d.])\d{{1,2}}\.\d{{1,2}}(?:\.\d{{4}})?{optional_time}(?![\d.])"]
        matches = [m for p in patterns for m in re.finditer(p,title,re.I)]
        if len(matches)>1:
            raise ValueError("Нашёл несколько сроков. Укажи один срок после / или назначь его кнопкой.")
        if matches:
            m = matches[0]
            due = parse_due(m.group(),now)
            title = (title[:m.start()]+title[m.end():]).strip(" ,.!-")
            title = re.sub(r"^напомни(?:ть)?(?:\s+мне)?\s*", "", title, flags=re.I).strip(" ,.!-")
            if not title:
                raise ValueError("Добавь, о чём напомнить.")
    return title, category, due


def parse_due(text, now):
    s = re.sub(r"\s+", " ", text.lower().strip().replace("ё", "е")).strip(" ,!")
    relative = re.fullmatch(r"через\s+(\d{1,4})\s*(мин(?:ут[уы]?)?|м|час(?:а|ов)?|ч|д(?:ень|ня|ней)?|д)", s)
    if relative:
        n, unit = relative.groups()
        n = int(n)
        if n < 1:
            raise ValueError("Интервал должен быть больше нуля.")
        delta = timedelta(minutes=n) if unit.startswith("м") else timedelta(hours=n) if unit.startswith("ч") else timedelta(days=n)
        return int((now + delta).timestamp())
    words = {"один":1,"два":2,"три":3,"четыре":4,"пять":5,"шесть":6,"семь":7,"восемь":8,"девять":9,"десять":10,"одиннадцать":11,"двенадцать":12}
    for word, number in words.items():
        s = re.sub(r"\b"+word+r"\b",str(number),s)
    s = re.sub(r"(?:\s+час(?:а|ов)?)$","",s)
    time_pattern = r"(?:\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?)?"
    def clock(hour, minute):
        try:
            return now.replace(hour=int(hour) if hour is not None else 9, minute=int(minute or 0), second=0,microsecond=0)
        except ValueError as exc:
            raise ValueError("Такого времени нет.") from exc
    def future(target):
        if target <= now:
            raise ValueError("Срок уже прошёл. Укажи будущую дату и время.")
        return int(target.timestamp())
    week = re.fullmatch(r"(?:в\s+)?(?:(этот|эту|это|следующий|следующую|следующее)\s+)?("+"|".join(WEEKDAYS)+r")"+time_pattern,s)
    if week:
        modifier,day,hour,minute = week.groups()
        target = clock(hour,minute)
        delta = WEEKDAYS[day]-now.weekday()
        if modifier and modifier.startswith('следующ'):
            target += timedelta(days=delta+7)
        elif modifier:
            target += timedelta(days=delta)
        else:
            target += timedelta(days=delta%7)
            if target<=now:
                target += timedelta(days=7)
        return future(target)
    named = re.fullmatch(r"(\d{1,2})\s+("+"|".join(MONTHS)+r")(?:\s+(\d{4})(?:\s*(?:года|г\.?))?)?"+time_pattern,s)
    numeric = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?"+time_pattern,s)
    if named or numeric:
        day,month,year,hour,minute = (named or numeric).groups()
        month = MONTHS[month] if named else int(month)
        base = clock(hour,minute)
        years = [int(year)] if year else range(now.year, min(now.year+9,10000))
        for candidate in years:
            try:
                target = base.replace(year=candidate,month=month,day=int(day))
            except ValueError:
                continue
            if target>now:
                return int(target.timestamp())
            if year:
                return future(target)
        raise ValueError("Такой будущей даты нет. Проверь число, месяц и год.")
    day = re.fullmatch(r"(сегодня|завтра|послезавтра)"+time_pattern,s)
    if day:
        name,hour,minute=day.groups()
        return future(clock(hour,minute)+timedelta(days={'сегодня':0,'завтра':1,'послезавтра':2}[name]))
    bare = re.fullmatch(r"(?:в\s+)?(\d{1,2})(?::(\d{2}))?",s)
    if bare:
        target=clock(*bare.groups())
        if target<=now:
            target+=timedelta(days=1)
        return future(target)
    raise ValueError("Не понял срок. Пример: 15 октября, в этот четверг, завтра 18:00 или через 30 минут. Без времени — 09:00 по Москве.")


REPEATS = {'daily': 'Раз в день', 'weekly': 'Раз в неделю', 'monthly': 'Раз в месяц', 'yearly': 'Раз в год'}


def next_repeat(due, kind, anchor, now, tz):
    current = datetime.fromtimestamp(due, tz)
    origin = datetime.fromtimestamp(anchor or due, tz)
    after = datetime.fromtimestamp(now, tz)
    if kind in ('daily', 'weekly'):
        days = 1 if kind == 'daily' else 7
        steps = max(1, (after.date()-current.date()).days // days)
        target = current + timedelta(days=days*steps)
        if target <= after:
            target += timedelta(days=days)
    elif kind == 'monthly':
        month = max(current.year*12+current.month, after.year*12+after.month-1)
        year, index = divmod(month, 12)
        target = current.replace(year=year, month=index+1, day=min(origin.day, calendar.monthrange(year,index+1)[1]))
        if target <= after:
            year,index = divmod(month+1,12)
            target = current.replace(year=year,month=index+1,day=min(origin.day,calendar.monthrange(year,index+1)[1]))
    elif kind == 'yearly':
        year = max(current.year+1,after.year)
        target = current.replace(year=year,month=origin.month,day=min(origin.day,calendar.monthrange(year,origin.month)[1]))
        if target <= after:
            year += 1
            target = current.replace(year=year,month=origin.month,day=min(origin.day,calendar.monthrange(year,origin.month)[1]))
    else:
        raise ValueError('Unknown repetition')
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
        if "repeat" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN repeat TEXT NOT NULL DEFAULT ''")
        if "repeat_anchor" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN repeat_anchor INTEGER")
        self.db.execute("UPDATE tasks SET category='Личное' WHERE category='Входящие'")
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
            return self.db.execute("UPDATE tasks SET due=?,repeat_anchor=?,notified=0,retry_at=0 WHERE user_id=? AND id=? AND done=0", (due,due,uid,tid)).rowcount

    def set_repeat(self, uid, tid, kind):
        task = self.get(uid,tid)
        if not task or task['done'] or kind not in ('', *REPEATS):
            raise ValueError('Недопустимое повторение.')
        if kind and not task['due']:
            raise ValueError('Сначала назначь дату и время первого напоминания.')
        with self.db:
            self.db.execute("UPDATE tasks SET repeat=?,repeat_anchor=? WHERE user_id=? AND id=?",(kind,task['due'],uid,tid))

    def advance_repeat(self, uid, tid, expected_due, now, tz):
        task = self.get(uid,tid)
        if not task or not task['repeat'] or not task['due'] or task['due'] != expected_due:
            return False
        due = next_repeat(task['due'],task['repeat'],task['repeat_anchor'],now,tz)
        with self.db:
            return bool(self.db.execute("UPDATE tasks SET due=?,notified=0,retry_at=0 WHERE user_id=? AND id=? AND due=? AND done=0",(due,uid,tid,expected_due)).rowcount)

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
