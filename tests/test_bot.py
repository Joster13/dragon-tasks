import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage,SendPhoto,SendVoice,EditMessageReplyMarkup
from aiogram.types import Message
from core import Store,parse_task,parse_due,next_repeat
from main import build_dispatcher,scheduler_tick,send_reminder
TZ=ZoneInfo('Europe/Moscow')
NOW=datetime(2026,10,12,10,0,tzinfo=TZ)

class FakeSession(BaseSession):
    def __init__(self):
        super().__init__(); self.calls=[]
    async def close(self): pass
    async def stream_content(self,*args,**kwargs): yield b'audio'
    async def make_request(self,bot,method,timeout=None):
        self.calls.append(method)
        if isinstance(method,(SendMessage,SendPhoto,SendVoice,EditMessageReplyMarkup)):
            return Message(message_id=len(self.calls),date=NOW,chat={'id':getattr(method,'chat_id',1),'type':'private'},from_user={'id':123456,'is_bot':True,'first_name':'Bot'},text=getattr(method,'text',None))
        return True

class CoreTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(parse_due('завтра 18:00',NOW),int(datetime(2026,10,13,18,tzinfo=TZ).timestamp()))
        self.assertEqual(parse_due('через 15 минут',NOW),int(NOW.timestamp())+900)
        self.assertEqual(parse_due('в пятницу в 15:00',NOW),int(datetime(2026,10,16,15,tzinfo=TZ).timestamp()))
        self.assertEqual(parse_due('завтра в десять',NOW),int(datetime(2026,10,13,10,tzinfo=TZ).timestamp()))
        for text in ['сегодня 09:00','32.10.2026 12:00','завтра 25:00','через 0 минут','как-нибудь']:
            with self.subTest(text=text),self.assertRaises(ValueError): parse_due(text,NOW)
    def test_natural_dates(self):
        title,category,due=parse_task('Напомни завтра в 18:00 купить аккумулятор #личное',NOW)
        self.assertEqual(title,'купить аккумулятор'); self.assertEqual(category,'Личное'); self.assertIsNotNone(due)
        self.assertIsNotNone(parse_task('Завтра в десять позвонить поставщику',NOW)[2])
        self.assertIsNone(parse_task('Купить 2 диска на 8 ТБ',NOW)[2])
        self.assertIsNone(parse_task('Купить диск 2.5 дюйма',NOW)[2])
    def test_slash_separator(self):
        title,category,due=parse_task('Купить кабель #работа / через 1 час',NOW)
        self.assertEqual((title,category,due),('Купить кабель','Работа',int(NOW.timestamp())+3600))
        self.assertEqual(parse_task('Начало работы на Денди #VPN/ завтра 10:00',NOW)[:2],('Начало работы на Денди','VPN'))
        title,category,due=parse_task('Проверить https://example.com/path / завтра 18:00',NOW)
        self.assertEqual(title,'Проверить https://example.com/path')
        self.assertIsNotNone(due)
        self.assertEqual(parse_task('Проверить https://example.com/path',NOW)[0],'Проверить https://example.com/path')
        with self.assertRaises(ValueError): parse_task('Задача / завтра 25:00',NOW)
    def test_named_dates_and_weekdays(self):
        def stamp(y,m,d,h=9): return int(datetime(y,m,d,h,tzinfo=TZ).timestamp())
        cases={'15 октября':stamp(2026,10,15),'15 октября в 18:00':stamp(2026,10,15,18),
               '15 октября 2027 года в десять':stamp(2027,10,15,10),'в этот четверг':stamp(2026,10,15),
               'в эту пятницу в 15:00':stamp(2026,10,16,15),'в следующую пятницу':stamp(2026,10,23),
               'завтра':stamp(2026,10,13),'15.10':stamp(2026,10,15),'29 февраля':stamp(2028,2,29),
               '1 января':stamp(2027,1,1)}
        for text,expected in cases.items():
            with self.subTest(text=text): self.assertEqual(parse_due(text,NOW),expected)
        for text in ['31 апреля','15 октября 2025','в этот понедельник','15 октября 25:00']:
            with self.subTest(text=text),self.assertRaises(ValueError): parse_due(text,NOW)
        for text in ['Купить продукты / 15 октября','15 октября Купить продукты','Купить продукты в этот четверг']:
            with self.subTest(text=text):
                self.assertEqual(parse_task(text,NOW),('Купить продукты','Личное',stamp(2026,10,15)))
    def test_repeat_calendar(self):
        def stamp(y,m,d): return int(datetime(y,m,d,10,tzinfo=TZ).timestamp())
        jan=stamp(2027,1,31)
        feb=next_repeat(jan,'monthly',jan,jan,TZ)
        self.assertEqual(feb,stamp(2027,2,28))
        self.assertEqual(next_repeat(feb,'monthly',jan,feb,TZ),stamp(2027,3,31))
        leap=stamp(2028,2,29)
        self.assertEqual(next_repeat(leap,'yearly',leap,leap,TZ),stamp(2029,2,28))
        self.assertEqual(next_repeat(stamp(2031,2,28),'yearly',leap,stamp(2031,2,28),TZ),stamp(2032,2,29))
        self.assertEqual(next_repeat(jan,'weekly',jan,stamp(2027,2,20),TZ),stamp(2027,2,21))
    def test_repeat_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'tasks.db'); store=Store(path)
            tid=store.add(1,'Задача','Личное',None,0)
            with self.assertRaises(ValueError): store.set_repeat(1,tid,'daily')
            due=int(NOW.timestamp()); store.snooze(1,tid,due); store.set_repeat(1,tid,'monthly')
            with self.assertRaises(ValueError): store.set_repeat(2,tid,'daily')
            store.attach(1,tid,'photo','photo1',1); store.close(); store=Store(path)
            self.assertEqual(store.get(1,tid)['repeat'],'monthly')
            self.assertTrue(store.advance_repeat(1,tid,due,due,TZ))
            self.assertFalse(store.advance_repeat(1,tid,due,due,TZ))
            self.assertEqual(len(store.attachments(tid)),1)
            store.snooze(1,tid,due+900); self.assertEqual(store.get(1,tid)['repeat_anchor'],due+900)
            store.set_repeat(1,tid,''); self.assertEqual(store.get(1,tid)['repeat'],''); store.close()
    def test_persistence_and_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'tasks.db'); store=Store(path)
            tid=store.add(1,'Задача','Работа',1,0)
            store.attach(1,tid,'photo','photo1',100); store.attach(1,tid,'photo','photo1',100)
            self.assertFalse(store.attach(2,tid,'photo','foreign',101))
            self.assertEqual(store.delete(2,tid),0); self.assertEqual(store.snooze(2,tid,999),0)
            store.input(1,tid,'custom'); store.edit(1,tid,draft=1)
            self.assertFalse(store.pending(100)); store.close(); store=Store(path)
            self.assertEqual(len(store.attachments(tid)),1); self.assertEqual(store.input(1)['task_id'],tid)
            store.edit(1,tid,draft=0); self.assertEqual(len(store.pending(100)),1)
            store.finish(1,tid); self.assertFalse(store.pending(100)); store.close()

class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(str(Path(self.tmp.name)/'bot.db'))
        self.session=FakeSession(); self.bot=Bot('123456:TEST_PLACEHOLDER_ONLY',session=self.session)
        self.dp=build_dispatcher(self.store,TZ,{1,2}); self.counter=0
    async def asyncTearDown(self):
        self.store.close(); await self.bot.session.close(); self.tmp.cleanup()
    async def message(self,uid=1,text=None,**fields):
        self.counter+=1
        msg={'message_id':self.counter,'date':int(NOW.timestamp()),'chat':{'id':uid,'type':'private'},'from':{'id':uid,'is_bot':False,'first_name':'User'},**fields}
        if text is not None:msg['text']=text
        await self.dp.feed_raw_update(self.bot,{'update_id':self.counter,'message':msg})
    async def callback(self,data,uid=1):
        self.counter+=1
        await self.dp.feed_raw_update(self.bot,{'update_id':self.counter,'callback_query':{'id':str(self.counter),'from':{'id':uid,'is_bot':False,'first_name':'User'},'chat_instance':'test','data':data,'message':{'message_id':50,'date':int(NOW.timestamp()),'chat':{'id':uid,'type':'private'},'from':{'id':123456,'is_bot':True,'first_name':'Bot'},'text':'Task'}}})
    async def test_photo_transfer_and_exactly_two_buttons(self):
        await self.message(photo=[{'file_id':'photo1','file_unique_id':'unique','width':100,'height':100}],caption='Купить кабель #работа')
        tid=self.store.list(1)[0]['id']; await self.callback(f'shift:{tid}:900')
        self.assertEqual(len(self.store.attachments(tid)),1)
        await send_reminder(self.bot,self.store.get(1,tid),self.store,TZ)
        call=self.session.calls[-1]; self.assertIsInstance(call,SendPhoto)
        self.assertEqual([b.text for row in call.reply_markup.inline_keyboard for b in row],['✅ Выполнено','📅 Перенести'])
        await self.callback(f'done:{tid}'); self.assertFalse(self.store.pending(9999999999))
    async def test_album_is_one_task(self):
        for n in range(3):
            await self.message(photo=[{'file_id':f'p{n}','file_unique_id':str(n),'width':100,'height':100}],media_group_id='album')
        self.assertEqual(len(self.store.list(1)),1); self.assertEqual(len(self.store.attachments(self.store.list(1)[0]['id'])),3)
    async def test_calendar_custom_time(self):
        await self.message(text='Сделать работу'); tid=self.store.list(1)[0]['id']
        await self.callback(f'day:{tid}:2030-10-15'); await self.message(text='15:30')
        self.assertEqual(datetime.fromtimestamp(self.store.get(1,tid)['due'],TZ),datetime(2030,10,15,15,30,tzinfo=TZ))
        self.assertIsNone(self.store.input(1))
    async def test_foreign_user_blocked(self):
        await self.message(text='Секрет'); tid=self.store.list(1)[0]['id']
        await self.callback(f'done:{tid}',uid=2); self.assertFalse(self.store.get(1,tid)['done'])
        await self.message(uid=3,text='/tasks'); self.assertNotIn('Секрет',self.session.calls[-1].text)
    async def test_scheduler_no_repeat(self):
        self.store.register(1); self.store.set_digest(1,'00:00'); tid=self.store.add(1,'Дело','Личное',1,0)
        await scheduler_tick(self.bot,self.store,TZ,{1}); count=len(self.session.calls)
        await scheduler_tick(self.bot,self.store,TZ,{1}); self.assertEqual(len(self.session.calls),count)
        self.assertEqual(self.store.get(1,tid)['notified'],1)
    async def test_recurring_reminder_and_completion(self):
        due=int(NOW.timestamp())-30
        tid=self.store.add(1,'Ежедневная','Личное',due,0)
        self.store.attach(1,tid,'photo','photo1',1)
        await self.callback(f'setrepeat:{tid}:daily')
        self.store.set_digest(1,'off')
        with patch('main.datetime', wraps=datetime) as clock:
            clock.now.return_value=NOW
            await scheduler_tick(self.bot,self.store,TZ,{1})
            next_due=self.store.get(1,tid)['due']
            self.assertEqual(next_due,due+86400)
            reminder=self.session.calls[-1]
            self.assertIsInstance(reminder,SendPhoto)
            self.assertEqual(len(reminder.reply_markup.inline_keyboard[0]),2)
            self.assertEqual(reminder.reply_markup.inline_keyboard[0][0].callback_data,f'done:{tid}:{due}')
            await self.callback(f'done:{tid}:{due}')
            self.assertEqual(self.store.get(1,tid)['due'],next_due)
            self.assertFalse(self.store.get(1,tid)['done'])
            count=len(self.session.calls)
            await scheduler_tick(self.bot,self.store,TZ,{1})
            self.assertEqual(len(self.session.calls),count)
            clock.now.return_value=NOW+timedelta(days=1)
            await scheduler_tick(self.bot,self.store,TZ,{1})
            self.assertEqual(self.store.get(1,tid)['due'],due+2*86400)
        await self.callback(f'setrepeat:{tid}:off')
        self.assertEqual(self.store.get(1,tid)['repeat'],'')
    async def test_manual_named_deadline(self):
        await self.message(text='Задача'); tid=self.store.list(1)[0]['id']
        await self.callback(f'custom:{tid}')
        with patch('main.datetime',wraps=datetime) as clock:
            clock.now.return_value=NOW
            await self.message(text='15 октября')
        self.assertEqual(datetime.fromtimestamp(self.store.get(1,tid)['due'],TZ),datetime(2026,10,15,9,tzinfo=TZ))
        await self.callback(f'custom:{tid}')
        with patch('main.datetime',wraps=datetime) as clock:
            clock.now.return_value=NOW
            await self.message(text='в этот четверг')
        self.assertEqual(datetime.fromtimestamp(self.store.get(1,tid)['due'],TZ),datetime(2026,10,15,9,tzinfo=TZ))
    async def test_start_and_help_are_separate(self):
        await self.message(text='/start')
        text=self.session.calls[-1].text
        self.assertIn('Dragon Tasks | Задачи',text)
        self.assertIn('Начало работы на Денди #VPN/ завтра 10:00',text)
        self.assertNotIn('/digest',text)
        await self.message(text='/help')
        self.assertIn('/digest',self.session.calls[-1].text)
        self.assertNotEqual(text,self.session.calls[-1].text)
    async def test_voice_requires_confirmation(self):
        await self.message(voice={'file_id':'v1','file_unique_id':'v','duration':5}); tid=self.store.list(1)[0]['id']
        with patch.dict('os.environ',{'OPENAI_API_KEY':'test'}),patch('main.transcribe',return_value='Купить кабель #работа / завтра 18:00') as mocked:
            await self.callback(f'speech:{tid}'); mocked.assert_not_called()
            await self.callback(f'speechok:{tid}'); mocked.assert_awaited_once()
        self.assertTrue(self.store.get(1,tid)['draft']); self.assertFalse(self.store.pending(9999999999))
        await self.callback(f'confirm:{tid}'); self.assertFalse(self.store.get(1,tid)['draft'])
        self.assertEqual(self.store.attachments(tid)[0]['kind'],'voice')

if __name__=='__main__': unittest.main()
