import asyncio
import io
import os
import threading
import time
from functools import wraps
from pathlib import Path

import open_webui.services.audio as audio_service
import pytest
from open_webui.services.audio import (
    AudioBusyError,
    AudioCacheManager,
    AudioModelError,
    AudioTimeoutError,
    AudioUnsupportedMediaError,
    AudioUploadTooLargeError,
    FasterWhisperService,
    configure_pydub_audio,
    ffmpeg_readiness,
    save_validated_upload,
)
from starlette.datastructures import Headers, UploadFile


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class Segment:
    text = ' hello'


class Info:
    language = 'en'
    language_probability = 0.99


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, file_path, **kwargs):
        self.calls.append(kwargs)
        return iter([Segment()]), Info()


def test_pydub_prefers_system_ffmpeg_and_reports_ffprobe_separately(monkeypatch):
    from pydub import AudioSegment

    system_ffmpeg = r'C:\tools\ffmpeg.exe'
    monkeypatch.setattr(
        audio_service.shutil,
        'which',
        lambda name: system_ffmpeg if name == 'ffmpeg' else None,
    )

    def bundled_must_not_be_used():
        raise AssertionError('bundled ffmpeg lookup should not run')

    monkeypatch.setattr('imageio_ffmpeg.get_ffmpeg_exe', bundled_must_not_be_used)

    readiness = configure_pydub_audio()

    assert AudioSegment.converter == system_ffmpeg
    assert readiness == {'ffmpeg': True, 'ffprobe': False}


def test_pydub_uses_bundled_ffmpeg_without_claiming_ffprobe(monkeypatch, tmp_path):
    from pydub import AudioSegment

    bundled_ffmpeg = tmp_path / 'ffmpeg.exe'
    bundled_ffmpeg.write_bytes(b'fake executable')
    monkeypatch.setattr(audio_service.shutil, 'which', lambda name: None)
    monkeypatch.setattr('imageio_ffmpeg.get_ffmpeg_exe', lambda: str(bundled_ffmpeg))

    readiness = ffmpeg_readiness()

    assert AudioSegment.converter == str(bundled_ffmpeg)
    assert readiness == {'ffmpeg': True, 'ffprobe': False}
    assert all(isinstance(value, bool) for value in readiness.values())


def test_ffmpeg_readiness_is_false_when_no_executable_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_service.shutil, 'which', lambda name: None)
    monkeypatch.setattr(
        'imageio_ffmpeg.get_ffmpeg_exe',
        lambda: str(tmp_path / 'missing-ffmpeg.exe'),
    )

    assert ffmpeg_readiness() == {'ffmpeg': False, 'ffprobe': False}


def make_service(
    tmp_path: Path,
    factory,
    *,
    concurrency=2,
    timeout=1,
    load_timeout=1,
    auto_update=False,
) -> FasterWhisperService:
    return FasterWhisperService(
        model_name='base',
        compute_type='int8',
        model_dir=str(tmp_path),
        device='cpu',
        auto_update=auto_update,
        concurrency=concurrency,
        timeout_seconds=timeout,
        load_timeout_seconds=load_timeout,
        vad_filter=True,
        model_factory=factory,
    )


def test_readiness_does_not_load_model(tmp_path):
    loads = 0

    def factory(**kwargs):
        nonlocal loads
        loads += 1
        return FakeModel()

    readiness = make_service(tmp_path, factory).readiness()

    assert readiness['loaded'] is False
    assert readiness['model'] == 'base'
    assert loads == 0


@async_test
async def test_whisper_load_is_single_and_language_is_optional(tmp_path):
    model = FakeModel()
    loads = 0

    def factory(**kwargs):
        nonlocal loads
        loads += 1
        time.sleep(0.03)
        return model

    service = make_service(tmp_path, factory)
    first, second = await asyncio.gather(
        service.transcribe('one.wav'),
        service.transcribe('two.wav', 'hi'),
    )

    assert loads == 1
    assert first.text == second.text == 'hello'
    assert 'language' not in model.calls[0]
    assert model.calls[1]['language'] == 'hi'
    assert all('multilingual' not in call for call in model.calls)


@async_test
async def test_safe_reload_keeps_working_model_on_failure(tmp_path):
    original = FakeModel()

    def factory(**kwargs):
        if kwargs['model_size_or_path'] == 'broken':
            raise RuntimeError('load failed')
        return original

    service = make_service(tmp_path, factory)
    await service.get_model()

    with pytest.raises(AudioModelError):
        await service.reload('broken')

    assert service.loaded
    assert service.model_name == 'base'
    assert await service.get_model() is original


@async_test
async def test_reload_swaps_model_and_unload_resets_readiness(tmp_path):
    created = {}

    def factory(**kwargs):
        model = FakeModel()
        created[kwargs['model_size_or_path']] = model
        return model

    service = make_service(tmp_path, factory)
    assert await service.get_model() is created['base']

    await service.reload('small')
    assert service.model_name == 'small'
    assert await service.get_model() is created['small']

    await service.unload()
    assert service.readiness()['loaded'] is False


@async_test
async def test_offline_model_load_never_retries_with_downloads_enabled(tmp_path):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('not cached')

    service = make_service(tmp_path, factory, auto_update=False)
    with pytest.raises(AudioModelError):
        await service.get_model()

    assert len(calls) == 1
    assert calls[0]['local_files_only'] is True


@async_test
async def test_model_load_timeout_has_stable_public_error(tmp_path):
    def factory(**kwargs):
        time.sleep(0.1)
        return FakeModel()

    service = make_service(tmp_path, factory, load_timeout=0.01)
    with pytest.raises(AudioTimeoutError) as exc_info:
        await service.get_model()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == 'Audio transcription timed out.'


@async_test
async def test_timeout_keeps_concurrency_slot_until_worker_finishes(tmp_path):
    finished = threading.Event()

    class SlowModel(FakeModel):
        def transcribe(self, file_path, **kwargs):
            time.sleep(1.1)
            finished.set()
            return super().transcribe(file_path, **kwargs)

    service = make_service(tmp_path, lambda **kwargs: SlowModel(), concurrency=1, timeout=1)
    transient = tmp_path / 'transient'
    transient.mkdir()
    (transient / 'slow.wav').write_bytes(b'audio')
    with pytest.raises(AudioTimeoutError):
        await service.transcribe('slow.wav', cleanup_directory=str(transient))

    assert service.active == 1
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    assert service.active == 0
    assert not transient.exists()


@async_test
async def test_concurrency_queue_times_out_as_busy(tmp_path):
    release = threading.Event()
    started = threading.Event()

    class BlockingModel(FakeModel):
        def transcribe(self, file_path, **kwargs):
            started.set()
            release.wait(1)
            return super().transcribe(file_path, **kwargs)

    service = make_service(tmp_path, lambda **kwargs: BlockingModel(), concurrency=1, timeout=0.05)
    first = asyncio.create_task(service.transcribe('first.wav'))
    assert await asyncio.to_thread(started.wait, 1)

    with pytest.raises(AudioBusyError) as exc_info:
        await service.transcribe('second.wav')

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == 'Audio transcription service is busy.'
    with pytest.raises(AudioTimeoutError):
        await first
    release.set()
    await asyncio.sleep(0.05)
    assert service.active == 0


def make_upload(data: bytes, filename='sample.wav', content_type='audio/wav') -> UploadFile:
    return UploadFile(
        io.BytesIO(data),
        filename=filename,
        headers=Headers({'content-type': content_type}),
    )


@async_test
async def test_upload_enforces_bound_and_removes_partial_file(tmp_path):
    destination = tmp_path / 'audio.wav'
    upload = make_upload(b'RIFFxxxxWAVE' + b'a' * 64)

    with pytest.raises(AudioUploadTooLargeError):
        await save_validated_upload(
            upload,
            destination,
            max_bytes=20,
            allowed_extensions={'wav'},
            supported_content_types=['audio/wav'],
        )

    assert not destination.exists()


@async_test
async def test_upload_rejects_mismatched_signature_with_415_error(tmp_path):
    destination = tmp_path / 'audio.wav'
    upload = make_upload(b'not a wave file')

    with pytest.raises(AudioUnsupportedMediaError) as exc_info:
        await save_validated_upload(
            upload,
            destination,
            max_bytes=1024,
            allowed_extensions={'wav'},
            supported_content_types=['audio/wav'],
        )

    assert exc_info.value.status_code == 415
    assert not destination.exists()


@async_test
async def test_upload_rejects_mismatched_extension_and_content_type(tmp_path):
    destination = tmp_path / 'audio.wav'
    upload = make_upload(b'RIFFxxxxWAVEdata', content_type='audio/mpeg')

    with pytest.raises(AudioUnsupportedMediaError):
        await save_validated_upload(
            upload,
            destination,
            max_bytes=1024,
            allowed_extensions={'wav'},
            supported_content_types=['audio/*'],
        )

    assert not destination.exists()


@async_test
async def test_upload_rejects_empty_files_and_closes_stream(tmp_path):
    destination = tmp_path / 'audio.wav'
    upload = make_upload(b'')

    with pytest.raises(AudioUnsupportedMediaError):
        await save_validated_upload(
            upload,
            destination,
            max_bytes=1024,
            allowed_extensions={'wav'},
            supported_content_types=['audio/*'],
        )

    assert not destination.exists()
    assert upload.file.closed


@async_test
async def test_upload_accepts_valid_wildcard_media_type(tmp_path):
    destination = tmp_path / 'audio.wav'
    upload = make_upload(b'RIFFxxxxWAVEdata')

    size = await save_validated_upload(
        upload,
        destination,
        max_bytes=1024,
        allowed_extensions={'wav'},
        supported_content_types=['audio/*'],
    )

    assert size == 16
    assert destination.read_bytes() == b'RIFFxxxxWAVEdata'


@async_test
async def test_cache_cleanup_applies_ttl_and_entry_bound(tmp_path):
    old = tmp_path / 'old.mp3'
    old.write_bytes(b'old')
    old.touch()
    very_old = time.time() - 100
    os.utime(old, (very_old, very_old))
    (tmp_path / 'new.mp3').write_bytes(b'new')
    (tmp_path / 'new.json').write_text('{}')

    cache = AudioCacheManager(tmp_path, ttl_seconds=10, max_entries=1)
    await cache.cleanup()

    assert not old.exists()
    assert (tmp_path / 'new.mp3').exists()
    assert cache.readiness()['writable'] is True


@async_test
async def test_cache_entry_bound_removes_audio_and_metadata_as_one_group(tmp_path):
    old_time = time.time() - 10
    for stem in ('old', 'new'):
        (tmp_path / f'{stem}.mp3').write_bytes(stem.encode())
        (tmp_path / f'{stem}.json').write_text('{}')
    os.utime(tmp_path / 'old.mp3', (old_time, old_time))
    os.utime(tmp_path / 'old.json', (old_time, old_time))

    await AudioCacheManager(tmp_path, ttl_seconds=0, max_entries=1).cleanup()

    assert not (tmp_path / 'old.mp3').exists()
    assert not (tmp_path / 'old.json').exists()
    assert (tmp_path / 'new.mp3').exists()
    assert (tmp_path / 'new.json').exists()
