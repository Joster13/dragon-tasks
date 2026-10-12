"""Dragon Tasks: private task manager with persistent conversations and attachments."""
import re
import asyncio
import calendar
import contextlib
import io
import json
import logging
import os
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, BotCommand)
from dotenv import load_dotenv
from core import Store, parse_task, parse_due, LISTS, REPEATS

load_dotenv()
log = logging.getLogger('dragon')
CATEGORIES = ['Учёба', 'Личное', 'Работа', 'VPN']
HELP = '''🐉 <b>Dragon Tasks</b>
Пришли текст, фото, документ, голосовое или перешли сообщение.
Можно написать: «Завтра в 18:00 купить аккумулятор».
Точный формат: «Проверить NAS #работа / завтра 10:00».

После сохранения назначь время и раздел кнопками. «🔁 Повторение»: раз в день, неделю, месяц или год. Без срока задача остаётся в списке, уведомления не будет.

В напоминании только <b>✅ Выполнено</b> и <b>📅 Перенести</b>.
Перенос: 15 минут, час, 3 часа, завтра или календарь и своё время.

/tasks — все задачи · /today — сегодня
/overdue — просроченные · /lists — разделы
/done — выполненные · /drafts — неподтверждённые
/digest 09:00 или /digest off — сводка
/export — выгрузка задач · /cancel — отменить ввод

Голосовое сохраняется всегда. Расшифровка — кнопкой с подтверждением отправки аудио в OpenAI, если владелец подключил API. Оригинал остаётся в задаче.'''


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows])


def reminder_keyboard(tid, occurrence=None):
    # Exactly two buttons on every reminder, including reminders with media.
    return markup([[('✅ Выполнено',f'done:{tid}' + (f':{occurrence}' if occurrence is not None else '')),('📅 Перенести',f'move:{tid}')]])


def task_keyboard(task):
    tid = task['id']
    rows = []
    if task['draft']:
        rows.append([('✅ Всё верно — сохранить',f'confirm:{tid}')])
    elif not task['done']:
        rows.append([('✅ Выполнено',f'done:{tid}:{task["due"]}' if task['repeat'] and task['due'] else f'done:{tid}'),('📅 Перенести',f'move:{tid}')])
    rows += [[('⏰ Назначить время',f'move:{tid}'),('📂 Раздел',f'category:{tid}')],
             [('✏️ Изменить текст',f'edit:{tid}'),('📎 Вложения',f'files:{tid}')],
             [('📎 Добавить вложения',f'attach:{tid}'),('🗑 Удалить',f'askdel:{tid}')]]
    if not task['done']:
        rows.append([('🔁 Повторение',f'repeat:{tid}')])
    return markup(rows)


def format_task(task,tz):
    due = datetime.fromtimestamp(task['due'],tz).strftime('%d.%m.%Y в %H:%M') if task['due'] else 'не назначено'
    status = '📝 Проверь задачу' if task['draft'] else '✅ Выполнено' if task['done'] else '🐉 Задача'
    repetition = '\n🔁 '+REPEATS[task['repeat']] if task['repeat'] else ''
    return f"<b>{status} #{task['id']}</b>\n{escape(task['title'])}\n\n📂 {escape(task['category'])}\n⏰ {due} · {tz.key}{repetition}"


def extract_attachment(message):
    if message.photo:
        return 'photo',message.photo[-1].file_id
    for kind in ('voice','document','video','audio','video_note'):
        media = getattr(message,kind,None)
        if media:
            return kind,media.file_id
    return None


async def send_files(bot,uid,files):
    methods = {'photo':bot.send_photo,'voice':bot.send_voice,'document':bot.send_document,
               'video':bot.send_video,'audio':bot.send_audio,'video_note':bot.send_video_note}
    for item in files:
        await methods[item['kind']](chat_id=uid,**{item['kind']:item['file_id']})


async def send_reminder(bot,task,store,tz):
    uid = task['user_id']
    files = store.attachments(task['id'])
    text = '⏰ <b>Напоминание</b>\n'+format_task(task,tz)
    if len(files)==1 and files[0]['kind']=='photo' and len(text)<=1024:
        await bot.send_photo(uid,files[0]['file_id'],caption=text,reply_markup=reminder_keyboard(task['id'], task['due'] if task['repeat'] else None))
    else:
        await send_files(bot,uid,files)
        await bot.send_message(uid,text,reply_markup=reminder_keyboard(task['id'], task['due'] if task['repeat'] else None))


class Access(BaseMiddleware):
    def __init__(self,allowed):
        self.allowed=allowed

    async def __call__(self,handler,event,data):
        uid = event.from_user.id if event.from_user else 0
        private = isinstance(event,Message) and event.chat.type=='private'
        if isinstance(event,CallbackQuery):
            private = isinstance(event.message,Message) and event.message.chat.type=='private'
        if not private or uid not in self.allowed:
            if isinstance(event,CallbackQuery):
                await event.answer('Нет доступа',show_alert=True)
            elif private:
                await event.answer(f'Бот личный. Твой Telegram ID: <code>{uid}</code>')
            return
        data['store'].register(uid)
        return await handler(event,data)


def calendar_keyboard(tid,year,month):
    rows = [[(f'{calendar.month_name[month]} {year}','noop')]]
    rows.append([(d,'noop') for d in ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']])
    for week in calendar.monthcalendar(year,month):
        rows.append([(str(day),f'day:{tid}:{year}-{month:02d}-{day:02d}') if day else ('·','noop') for day in week])
    prev = datetime(year,month,1)-timedelta(days=1)
    nxt = datetime(year+1,1,1) if month==12 else datetime(year,month+1,1)
    rows.append([('←',f'cal:{tid}:{prev.year}:{prev.month}'),('→',f'cal:{tid}:{nxt.year}:{nxt.month}')])
    rows.append([('✍️ Написать дату и время',f'custom:{tid}'),('Отмена',f'back:{tid}')])
    return markup(rows)


async def transcribe(bot,file_id,key):
    buf = io.BytesIO()
    await bot.download(file_id,destination=buf)
    form = aiohttp.FormData()
    form.add_field('file',buf.getvalue(),filename='voice.ogg',content_type='audio/ogg')
    form.add_field('model',os.getenv('TRANSCRIPTION_MODEL','gpt-4o-mini-transcribe'))
    form.add_field('language','ru')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        async with session.post('https://api.openai.com/v1/audio/transcriptions',headers={'Authorization':f'Bearer {key}'},data=form) as response:
            if response.status!=200:
                raise RuntimeError(f'Transcription HTTP {response.status}')
            body = await response.json()
            text = body.get('text','').strip()
            if not text or len(text)>1500:
                raise ValueError('Нужен текст от 1 до 1500 символов. Попробуй короткое голосовое.')
            return text


def build_dispatcher(store,tz,allowed):
    dp = Dispatcher(store=store,tz=tz)
    dp.message.outer_middleware(Access(allowed))
    dp.callback_query.outer_middleware(Access(allowed))

    async def card(bot,uid,tid,prefix=''):
        task = store.get(uid,tid)
        if task:
            files = store.attachments(tid)
            suffix = f'\n📎 Вложений: {len(files)}' if files else ''
            await bot.send_message(uid,prefix+format_task(task,tz)+suffix,reply_markup=task_keyboard(task))

    async def show(bot,uid,mode='all',category=None,offset=0):
        tasks = store.list(uid,mode,category,datetime.now(tz),limit=9,offset=offset)
        if not tasks:
            await bot.send_message(uid,'Здесь пока нет задач. Можно выдохнуть 🐉')
        for task in tasks[:8]:
            await card(bot,uid,task['id'])
        if len(tasks)>8:
            await bot.send_message(uid,'Следующая страница:',reply_markup=markup([[('Дальше →',f'page:{mode}:{category or "-"}:{offset+8}')]]))

    @dp.message(Command('start','help'))
    async def start(message:Message):
        await message.answer(HELP,reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text='📅 Сегодня'),KeyboardButton(text='📋 Все задачи')],
            [KeyboardButton(text='📂 Разделы'),KeyboardButton(text='⚠️ Просроченные')]],resize_keyboard=True))

    @dp.message(Command('cancel'))
    async def cancel(message:Message):
        store.clear_input(message.from_user.id)
        await message.answer('Ввод отменён. Сама задача сохранена.')

    @dp.message(Command('id'))
    async def identity(message:Message):
        await message.answer(f'<code>{message.from_user.id}</code>')

    @dp.message(Command('tasks','today','overdue','done','drafts'))
    async def lists(message:Message,bot:Bot):
        store.clear_input(message.from_user.id)
        cmd=message.text.split()[0].split('@')[0][1:]
        await show(bot,message.from_user.id,'all' if cmd=='tasks' else cmd)

    @dp.message(Command('lists'))
    @dp.message(F.text=='📂 Разделы')
    async def categories(message:Message):
        store.clear_input(message.from_user.id)
        await message.answer('Выбери раздел:',reply_markup=markup([[(c,f'list:{c}')] for c in CATEGORIES]))

    @dp.message(F.text.in_({'📅 Сегодня','📋 Все задачи','⚠️ Просроченные'}))
    async def menu(message:Message,bot:Bot):
        store.clear_input(message.from_user.id)
        await show(bot,message.from_user.id,{'📅 Сегодня':'today','📋 Все задачи':'all','⚠️ Просроченные':'overdue'}[message.text])

    @dp.message(Command('digest'))
    async def digest(message:Message):
        args=message.text.split(maxsplit=1)
        value=args[1].strip() if len(args)>1 else '09:00'
        if value!='off':
            try:
                value=datetime.strptime(value,'%H:%M').strftime('%H:%M')
            except ValueError:
                await message.answer('Пример: /digest 09:00 или /digest off')
                return
        store.set_digest(message.from_user.id,value)
        await message.answer('Сводка выключена.' if value=='off' else f'Сводка ежедневно в {value} ({tz.key}).')

    @dp.message(Command('export'))
    async def export(message:Message):
        content=json.dumps(store.export(message.from_user.id),ensure_ascii=False,indent=2).encode()
        await message.answer_document(BufferedInputFile(content,filename='dragon-tasks.json'))

    @dp.callback_query()
    async def action(query:CallbackQuery,bot:Bot):
        parts=(query.data or '').split(':')
        command=parts[0]
        uid=query.from_user.id
        if command=='noop':
            await query.answer()
            return
        if command in ('list','page'):
            try:
                if command=='list':
                    if parts[1] not in CATEGORIES:
                        raise ValueError
                    await query.answer()
                    await show(bot,uid,category=parts[1])
                else:
                    mode,category,offset=parts[1:]
                    offset=int(offset)
                    if mode not in ('all','today','overdue','done','drafts') or offset<0:
                        raise ValueError
                    await query.answer()
                    await show(bot,uid,mode,None if category=='-' else category,offset)
            except (ValueError,IndexError):
                await query.answer('Не понял страницу')
            return
        try:
            tid=int(parts[1])
        except (ValueError,IndexError):
            await query.answer('Не понял действие')
            return
        task=store.get(uid,tid)
        if not task:
            await query.answer('Задача не найдена',show_alert=True)
            return
        await query.answer()
        if command=='done':
            if task['draft']:
                await bot.send_message(uid,'Сначала подтверди задачу.')
                return
            if task['repeat']:
                try:
                    occurrence = int(parts[2]) if len(parts)>2 else task['due']
                except ValueError:
                    return
                store.advance_repeat(uid,tid,occurrence,int(datetime.now(tz).timestamp()),tz)
                store.clear_input(uid)
                await query.message.edit_reply_markup(reply_markup=None)
                await card(bot,uid,tid,'✅ Выполнено. Повторение продолжается.\n')
                return
            store.finish(uid,tid)
            store.clear_input(uid)
            await query.message.edit_reply_markup(reply_markup=None)
            await bot.send_message(uid,f'✅ Задача #{tid} выполнена.')
        elif command=='repeat':
            await bot.send_message(uid,'Как часто напоминать? Повторение идёт автоматически, даже если не нажимать «Выполнено».',reply_markup=markup(
                [[(label,f'setrepeat:{tid}:{kind}')] for kind,label in REPEATS.items()] +
                [[('Один раз (выключить повторение)',f'setrepeat:{tid}:off')],[('Назад',f'back:{tid}')]]))
        elif command=='setrepeat':
            try:
                kind = parts[2]
                store.set_repeat(uid,tid,'' if kind=='off' else kind)
                await card(bot,uid,tid,'Повторение обновлено.\n')
            except (ValueError,IndexError) as exc:
                await bot.send_message(uid,str(exc) if isinstance(exc,ValueError) else 'Выбери период кнопкой.')
        elif command=='move':
            if task['done']:
                await bot.send_message(uid,'Задача уже выполнена.')
                return
            await bot.send_message(uid,'Когда напомнить?',reply_markup=markup([
                [('Через 15 минут',f'shift:{tid}:900'),('Через час',f'shift:{tid}:3600')],
                [('Через 3 часа',f'shift:{tid}:10800'),('Завтра в это время',f'shift:{tid}:86400')],
                [('Сегодня вечером (20:00)',f'evening:{tid}')],
                [('📅 Другая дата и время',f'cal:{tid}:{datetime.now(tz).year}:{datetime.now(tz).month}')],
                [('✍️ Написать срок',f'custom:{tid}'),('Без напоминания',f'nodue:{tid}')],
                [('Отмена',f'back:{tid}')]]))
        elif command in ('shift','evening','nodue','time'):
            if task['done']:
                await bot.send_message(uid,'Задача уже выполнена.')
                return
            try:
                if command=='shift':
                    seconds=int(parts[2])
                    if seconds not in (900,3600,10800,86400):
                        raise ValueError
                    due=int(datetime.now(tz).timestamp())+seconds
                elif command=='evening':
                    due=parse_due('сегодня 20:00',datetime.now(tz))
                elif command=='nodue':
                    due=None
                else:
                    due=parse_due(datetime.strptime(parts[2],'%Y-%m-%d').strftime('%d.%m.%Y')+' '+parts[3]+':'+parts[4],datetime.now(tz))
                store.snooze(uid,tid,due)
                store.clear_input(uid)
                await card(bot,uid,tid,'Время обновлено.\n')
            except (ValueError,IndexError):
                await bot.send_message(uid,'Время уже прошло или указано неверно. Выбери другое.')
        elif command=='cal':
            try:
                year,month=int(parts[2]),int(parts[3])
                if not 2020<=year<=2100 or not 1<=month<=12:
                    raise ValueError
                await bot.send_message(uid,'Выбери день:',reply_markup=calendar_keyboard(tid,year,month))
            except (ValueError,IndexError):
                await bot.send_message(uid,'Не понял дату.')
        elif command=='day':
            try:
                day=datetime.strptime(parts[2],'%Y-%m-%d')
            except (ValueError,IndexError):
                return
            store.input(uid,tid,'clock|'+parts[2])
            await bot.send_message(uid,'Выбрано '+day.strftime('%d.%m.%Y')+'. Выбери время или напиши, например, 15:30:',reply_markup=markup([
                [(h,f'time:{tid}:{parts[2]}:{h}') for h in ['09:00','12:00','15:00']],
                [(h,f'time:{tid}:{parts[2]}:{h}') for h in ['18:00','20:00','22:00']],
                [('Отмена',f'back:{tid}')]]))
        elif command in ('custom','edit','attach'):
            store.input(uid,tid,command)
            text={'custom':'Напиши срок: завтра 18:00, в пятницу в 15:00 или 25.10.2026 09:30.',
                  'edit':'Пришли новое название задачи (до 1500 символов).',
                  'attach':'Отправь фото, альбом, голосовое или файл. Затем нажми «Закончить». Следующие сообщения добавляются к этой задаче.'}[command]
            await bot.send_message(uid,text,reply_markup=markup([[('Закончить' if command=='attach' else 'Отмена',f'back:{tid}')]]))
        elif command=='category':
            await bot.send_message(uid,'Выбери раздел:',reply_markup=markup([[(c,f'setcat:{tid}:{i}')] for i,c in enumerate(CATEGORIES)]))
        elif command=='setcat':
            try:
                idx=int(parts[2])
                if not 0<=idx<len(CATEGORIES):
                    raise ValueError
                store.edit(uid,tid,category=CATEGORIES[idx])
                await card(bot,uid,tid)
            except (ValueError,IndexError):
                return
        elif command=='confirm':
            store.edit(uid,tid,draft=0)
            await query.message.edit_reply_markup(reply_markup=None)
            await card(bot,uid,tid,'Сохранено.\n')
        elif command=='back':
            store.clear_input(uid)
            await card(bot,uid,tid)
        elif command=='files':
            files=store.attachments(tid)
            await send_files(bot,uid,files)
            if not files:
                await bot.send_message(uid,'Вложений пока нет.')
        elif command=='askdel':
            await bot.send_message(uid,'Удалить задачу и все её вложения?',reply_markup=markup([[('Удалить',f'delete:{tid}'),('Отмена',f'back:{tid}')]]))
        elif command=='delete':
            store.delete(uid,tid)
            await query.message.edit_reply_markup(reply_markup=None)
            await bot.send_message(uid,'🗑 Задача удалена.')
        elif command in ('speech','speechok'):
            key=os.getenv('OPENAI_API_KEY','').strip()
            voices=[f for f in store.attachments(tid) if f['kind']=='voice']
            if not key:
                await bot.send_message(uid,'Распознавание пока не подключено. Аудио сохранено; название и срок можно задать кнопками.')
                return
            if not voices:
                await bot.send_message(uid,'В задаче нет голосового.')
                return
            if command=='speech':
                await bot.send_message(uid,'Отправить последнее голосовое из этой задачи в OpenAI для расшифровки? Используется оплачиваемый API владельца бота.',reply_markup=markup([[('Распознать',f'speechok:{tid}'),('Отмена',f'back:{tid}')]]))
                return
            await bot.send_message(uid,'🎙 Распознаю…')
            try:
                transcript=await transcribe(bot,voices[-1]['file_id'],key)
                try:
                    title,category,due=parse_task(transcript,datetime.now(tz))
                except ValueError:
                    title,category,due=transcript,task['category'],None
                # Store draft; no reminders are changed or activated without confirmation.
                store.edit(uid,tid,title=title,category=category if any(tag.lower() in LISTS for tag in re.findall(r'#([\wёЁ]+)', transcript)) else task['category'],draft=1)
                if due:
                    store.snooze(uid,tid,due)
                await card(bot,uid,tid,'Распознал. Проверь текст и срок.\n')
            except (TelegramAPIError,aiohttp.ClientError,asyncio.TimeoutError,RuntimeError,ValueError):
                await bot.send_message(uid,'Не удалось распознать. Аудио сохранено. Можно повторить или задать текст вручную.')

    @dp.message()
    async def receive(message:Message,bot:Bot):
        uid=message.from_user.id
        text=message.text or message.caption or ''
        if text.startswith('/'):
            await message.answer('Неизвестная команда. /help — справка; /cancel — отменить текущий ввод.')
            return
        attachment=extract_attachment(message)
        pending=store.input(uid)
        if pending:
            tid=pending['task_id']
            task=store.get(uid,tid)
            if not task:
                store.clear_input(uid)
            else:
                action=pending['action']
                try:
                    if action=='attach':
                        if not attachment:
                            await message.answer('Нужно вложение. Чтобы добавлять новые задачи, нажми «Закончить» или /cancel.')
                            return
                        store.attach(uid,tid,*attachment,message.message_id)
                        await message.answer('📎 Добавлено.',reply_markup=markup([[('Закончить',f'back:{tid}')]]))
                        return
                    if not text:
                        raise ValueError('Пришли текст или /cancel.')
                    if action=='edit':
                        if len(text)>1500:
                            raise ValueError('Максимум 1500 символов.')
                        store.edit(uid,tid,title=text)
                    else:
                        if task['done']:
                            raise ValueError('Задача уже выполнена. /cancel — отменить ввод.')
                        deadline=text
                        if action.startswith('clock|'):
                            day=datetime.strptime(action.split('|')[1],'%Y-%m-%d').strftime('%d.%m.%Y')
                            deadline=day+' '+text
                        store.snooze(uid,tid,parse_due(deadline,datetime.now(tz)))
                    store.clear_input(uid)
                    await card(bot,uid,tid,'Обновлено.\n')
                except ValueError as exc:
                    await message.answer(escape(str(exc)))
                return
        if not text and not attachment:
            await message.answer('Пришли текст, фото, голосовое, видео или документ.')
            return
        # A Telegram media album is one task, persisted before the next update.
        if message.media_group_id:
            tid=store.group(uid,message.media_group_id)
            if tid:
                if attachment:
                    store.attach(uid,tid,*attachment,message.message_id)
                if text and store.get(uid,tid)['title']=='Фото / вложения':
                    try:
                        title,category,due=parse_task(text,datetime.now(tz))
                        store.edit(uid,tid,title=title,category=category,draft=int(due is not None))
                        if due:
                            store.snooze(uid,tid,due)
                        await card(bot,uid,tid)
                    except ValueError:
                        await message.answer('Не понял срок в подписи. Фото сохранены; назначь время кнопкой.')
                return
        warning=''
        try:
            title,category,due=parse_task(text,datetime.now(tz)) if text else ('Голосовая задача' if message.voice else 'Фото / вложения','Личное',None)
        except ValueError as exc:
            if len(text)>1500:
                await message.answer(escape(str(exc)))
                return
            title,category,due=text or 'Вложение','Личное',None
            warning='⚠️ '+escape(str(exc))+' Сохранил без срока.\n'
        tid=store.add(uid,title,category,due,int(datetime.now(tz).timestamp()))
        if due:
            store.edit(uid,tid,draft=1)
        if attachment:
            store.attach(uid,tid,*attachment,message.message_id)
        if message.media_group_id:
            store.group(uid,message.media_group_id,tid)
        await card(bot,uid,tid,warning)
        if message.voice:
            await message.answer('Оригинал аудио сохранён.',reply_markup=markup([[('🎙 Распознать речь',f'speech:{tid}')]]))

    return dp


async def scheduler_tick(bot,store,tz,allowed):
    now=datetime.now(tz)
    ts=int(now.timestamp())
    for task in store.pending(ts):
        if task['user_id'] not in allowed:
            continue
        try:
            await send_reminder(bot,task,store,tz)
        except TelegramForbiddenError:
            store.retry(task['id'],ts+86400)
        except TelegramRetryAfter as exc:
            store.retry(task['id'],ts+max(30,int(exc.retry_after)))
        except TelegramAPIError:
            store.retry(task['id'],ts+60)
            log.warning('Reminder delivery failed; retry scheduled')
        else:
            if task['repeat']:
                store.advance_repeat(task['user_id'],task['id'],task['due'],ts,tz)
            else:
                store.sent(task['id'])
    today=now.strftime('%Y-%m-%d')
    for user in store.digest_users():
        uid=user['id']
        if uid not in allowed or user['last_digest']==today or user['retry_at']>ts or now.strftime('%H:%M')<user['digest']:
            continue
        tasks=store.list(uid,'today',now=now,limit=6)
        late=store.list(uid,'overdue',now=now,limit=1)
        text='🐉 <b>План на '+now.strftime('%d.%m')+'</b>\n'
        # Summary titles are truncated to keep Telegram's message limit.
        text+='\n'.join(f"• #{t['id']} {escape(t['title'][:200])}" for t in tasks[:5]) if tasks else 'На сегодня задач со сроком нет.'
        if len(tasks)>5:
            text+='\nЕщё задачи: /today'
        if late:
            text+='\n⚠️ Есть просроченные: /overdue'
        text+='\nВсе задачи: /tasks'
        try:
            await bot.send_message(uid,text)
        except TelegramRetryAfter as exc:
            store.digest_retry(uid,ts+max(30,int(exc.retry_after)))
        except TelegramForbiddenError:
            store.digest_retry(uid,ts+86400)
        except TelegramAPIError:
            store.digest_retry(uid,ts+60)
        else:
            store.digest_sent(uid,today)


async def scheduler(bot,store,tz,allowed):
    while True:
        try:
            await scheduler_tick(bot,store,tz,allowed)
        except Exception as exc:
            log.error('Scheduler error: %s',type(exc).__name__)
        await asyncio.sleep(15)


async def main():
    token=os.getenv('BOT_TOKEN','').strip()
    allowed={int(x.strip()) for x in os.getenv('ALLOWED_USER_IDS','').split(',') if x.strip()}
    if not token or not allowed or any(uid<=0 for uid in allowed):
        raise RuntimeError('Задай BOT_TOKEN и положительные ALLOWED_USER_IDS в окружении хостинга.')
    tz=ZoneInfo(os.getenv('TIMEZONE','Europe/Moscow'))
    store=Store(os.getenv('DB_PATH','data/tasks.sqlite3'))
    bot=Bot(token,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp=build_dispatcher(store,tz,allowed)
    worker=None
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands([BotCommand(command=c,description=d) for c,d in [
            ('tasks','Все задачи'),('today','Сегодня'),('overdue','Просроченные'),('lists','Разделы'),
            ('drafts','Проверь распознанные задачи'),('digest','Настроить сводку'),('done','Выполненные'),
            ('export','Выгрузить задачи'),('cancel','Отменить ввод'),('help','Справка')]])
        worker=asyncio.create_task(scheduler(bot,store,tz,allowed))
        await dp.start_polling(bot,handle_as_tasks=False,close_bot_session=False)
    finally:
        if worker:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await bot.session.close()
        store.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s: %(message)s')
    asyncio.run(main())
