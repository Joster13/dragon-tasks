import asyncio
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import speech


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_audio_deleted_after_success_and_failure(self):
        saved=[]
        class FakeBot:
            async def get_file(self, file_id):
                return SimpleNamespace(file_size=5)
            async def download(self, info, destination):
                destination.write_bytes(b'audio')
                saved.append(destination)
        for result,error in [({'text':'купить молоко'},None),(None,ValueError('Не разобрал речь'))]:
            with self.subTest(failure=error is not None),patch('speech.run_worker',new_callable=AsyncMock,return_value=result,side_effect=error):
                if error:
                    with self.assertRaises(ValueError): await speech.transcribe(FakeBot(),'voice','cache')
                else:
                    self.assertEqual(await speech.transcribe(FakeBot(),'voice','cache'),'купить молоко')
            self.assertFalse(saved[-1].exists())
    async def test_large_audio_not_downloaded(self):
        bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_size=speech.MAX_AUDIO_BYTES+1)),download=AsyncMock())
        with self.assertRaises(ValueError): await speech.transcribe(bot,'voice','cache')
        bot.download.assert_not_awaited()
    def test_corrupt_model_not_extracted(self):
        with tempfile.TemporaryDirectory() as cache,patch('speech.urllib.request.urlopen',return_value=io.BytesIO(b'corrupt archive')):
            with self.assertRaisesRegex(RuntimeError,'checksum'): speech.ensure_model(cache)
            self.assertFalse((Path(cache)/speech.MODEL_NAME).exists())


if __name__=='__main__': unittest.main()
