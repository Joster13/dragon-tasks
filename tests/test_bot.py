import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage,SendPhoto,SendVoice,EditMessageReplyMarkup
from aiogram.types import Message
from core import Store,parse_task,parse_due
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
    async def test_voice_requires_confirmation(self):
        await self.message(voice={'file_id':'v1','file_unique_id':'v','duration':5}); tid=self.store.list(1)[0]['id']
        with patch.dict('os.environ',{'OPENAI_API_KEY':'test'}),patch('main.transcribe',return_value='Купить кабель #работа | завтра 18:00') as mocked:
            await self.callback(f'speech:{tid}'); mocked.assert_not_called()
            await self.callback(f'speechok:{tid}'); mocked.assert_awaited_once()
        self.assertTrue(self.store.get(1,tid)['draft']); self.assertFalse(self.store.pending(9999999999))
        await self.callback(f'confirm:{tid}'); self.assertFalse(self.store.get(1,tid)['draft'])
        self.assertEqual(self.store.attachments(tid)[0]['kind'],'voice')

if __name__=='__main__': unittest.main()
