"""Free Russian ASR: official Vosk model, local ffmpeg and isolated workers."""
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

MODEL_NAME = 'vosk-model-small-ru-0.22'
MODEL_URL = 'https://alphacephei.com/vosk/models/' + MODEL_NAME + '.zip'
MODEL_SHA256 = '961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4'
MAX_SECONDS = 180
MAX_AUDIO_BYTES = 10 * 1024 * 1024
_ASR_LOCK = asyncio.Lock()


def ensure_model(cache):
    """Download once into persistent data; never extract an unchecked archive."""
    cache = Path(cache)
    destination = cache / MODEL_NAME
    if (destination / '.ready').is_file() and (destination / 'am/final.mdl').is_file():
        return destination
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.vosk-', dir=cache) as temp:
        archive = Path(temp) / 'model.zip'
        checksum = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(MODEL_URL, timeout=30) as response, archive.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 60 * 1024 * 1024:
                    raise RuntimeError('Model download exceeds limit')
                checksum.update(chunk)
                output.write(chunk)
        if checksum.hexdigest() != MODEL_SHA256:
            raise RuntimeError('Model checksum mismatch')
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            if sum(item.file_size for item in members) > 300 * 1024 * 1024:
                raise RuntimeError('Model archive exceeds limit')
            for item in members:
                parts = Path(item.filename).parts
                if not parts or parts[0] != MODEL_NAME or '..' in parts or Path(item.filename).is_absolute():
                    raise RuntimeError('Invalid model archive path')
            package.extractall(temp)
        source = Path(temp) / MODEL_NAME
        if not (source / 'am/final.mdl').is_file():
            raise RuntimeError('Incomplete model')
        (source / '.ready').write_text(MODEL_SHA256, encoding='ascii')
        # Only this single ASR worker is active at a time.
        if destination.exists():
            import shutil
            shutil.rmtree(destination)
        source.rename(destination)
    return destination


def recognize_file(path, cache):
    import imageio_ffmpeg
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    model_path = ensure_model(cache)
    model = Model(str(model_path))
    recognizer = KaldiRecognizer(model, 16000)
    command = [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-protocol_whitelist', 'file,pipe', '-i', str(path), '-t', str(MAX_SECONDS+1),
               '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1']
    pieces = []
    total = 0
    decoder = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while data := decoder.stdout.read(8000):
            total += len(data)
            if total > MAX_SECONDS * 16000 * 2:
                raise ValueError('Голосовое длиннее 3 минут. Раздели его на несколько сообщений.')
            if recognizer.AcceptWaveform(data):
                pieces.append(json.loads(recognizer.Result()).get('text', ''))
        if decoder.wait() != 0:
            raise ValueError('Не удалось прочитать аудио. Отправь обычное голосовое Telegram.')
        pieces.append(json.loads(recognizer.FinalResult()).get('text', ''))
    finally:
        if decoder.poll() is None:
            decoder.kill()
            decoder.wait()
        decoder.stdout.close()
    text = ' '.join(part for part in pieces if part).strip()
    if not text:
        raise ValueError('Не разобрал речь. Попробуй записать ближе к микрофону и без фонового шума.')
    if len(text) > 1500:
        raise ValueError('Получилось больше 1500 символов. Раздели голосовое на несколько задач.')
    return text


async def run_worker(cache, audio=None):
    args = [sys.executable, str(Path(__file__).resolve()), '--worker', str(cache)]
    if audio is not None:
        args.append(str(audio))
    environment = {key: os.environ[key] for key in ('PATH', 'PYTHONPATH', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if key in os.environ}
    environment.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', PYTHONUNBUFFERED='1')
    async with _ASR_LOCK:
        worker = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=environment, start_new_session=True)
        try:
            output, _ = await asyncio.wait_for(worker.communicate(), timeout=180)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(worker.pid, signal.SIGKILL)
            await worker.wait()
            raise
    if worker.returncode != 0 or len(output) > 10000:
        raise RuntimeError('Распознавание остановлено. Аудио сохранено; попробуй ещё раз.')
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('Неверный ответ распознавания.') from exc
    if result.get('error'):
        raise ValueError(result['error'])
    return result


async def transcribe(bot, file_id, cache):
    info = await bot.get_file(file_id)
    if info.file_size and info.file_size > MAX_AUDIO_BYTES:
        raise ValueError('Голосовое больше 10 МБ. Раздели его на несколько сообщений.')
    # Temporary audio is private and deleted even if conversion fails or times out.
    with tempfile.TemporaryDirectory(prefix='dragon-voice-') as temp:
        audio = Path(temp) / 'voice.ogg'
        await bot.download(info, destination=audio)
        if audio.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError('Голосовое больше 10 МБ.')
        result = await run_worker(cache, audio)
        return result['text']


async def prepare(cache):
    """Preload/download and verify both engine and model before deployment is considered ready."""
    return await run_worker(cache)


if __name__ == '__main__':
    try:
        if len(sys.argv) not in (3, 4) or sys.argv[1] != '--worker':
            raise ValueError('Invalid worker arguments')
        if len(sys.argv) == 4:
            result = {'text': recognize_file(sys.argv[3], sys.argv[2])}
        else:
            import imageio_ffmpeg
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            model = Model(str(ensure_model(sys.argv[2])))
            result = {'ready': True, 'engine': 'Vosk', 'language': 'ru'}
        print(json.dumps(result, ensure_ascii=False))
    except ValueError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
    except Exception:
        # No file paths, transcript or credentials in operational logs.
        print(json.dumps({'error': 'Движок ещё не готов или временно недоступен. Аудио сохранено; попробуй распознать ещё раз.'}, ensure_ascii=False))
