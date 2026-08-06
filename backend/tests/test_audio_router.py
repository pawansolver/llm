import asyncio
import io
import os
from functools import wraps
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key-with-at-least-32-characters')

from open_webui.routers import audio  # noqa: E402


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        headers={},
        client=SimpleNamespace(host='127.0.0.1'),
    )


def upload() -> UploadFile:
    return UploadFile(
        io.BytesIO(b'RIFFxxxxWAVEdata'),
        filename='recording.wav',
        headers=Headers({'content-type': 'audio/wav'}),
    )


def test_codec_inspection_skips_mediainfo_without_ffprobe(monkeypatch, tmp_path):
    audio_file = tmp_path / 'recording.webm'
    audio_file.write_bytes(b'audio')
    monkeypatch.setattr(audio, 'ffmpeg_readiness', lambda: {'ffmpeg': True, 'ffprobe': False})

    def unexpected_mediainfo(_path):
        raise AssertionError('mediainfo requires ffprobe and must not be called')

    monkeypatch.setattr(audio, 'mediainfo', unexpected_mediainfo)

    assert audio.is_audio_conversion_required(str(audio_file)) is False


def test_audio_admin_routes_require_authentication():
    app = FastAPI()
    app.include_router(audio.router, prefix='/audio')

    with TestClient(app) as client:
        assert client.get('/audio/config').status_code in {401, 403}
        assert client.get('/audio/health').status_code in {401, 403}


@async_test
async def test_stt_and_tts_rbac_denials_are_stable(monkeypatch):
    async def get_config(key, default=None):
        values = {
            'audio.tts.engine': 'openai',
            'user.permissions': {'chat': {'stt': False, 'tts': False}},
        }
        return values.get(key, default)

    async def denied(*args, **kwargs):
        return False

    async def no_cleanup():
        return None

    monkeypatch.setattr(audio.Config, 'get', get_config)
    monkeypatch.setattr(audio, 'has_permission', denied)
    monkeypatch.setattr(audio.speech_cache, 'cleanup', no_cleanup)
    user = SimpleNamespace(id='user-1', role='user')

    with pytest.raises(HTTPException) as stt_error:
        await audio.transcription(request(), upload(), user=user)
    with pytest.raises(HTTPException) as tts_error:
        await audio.speech(request(), user=user)

    assert (stt_error.value.status_code, tts_error.value.status_code) == (403, 403)
    assert stt_error.value.detail == tts_error.value.detail


@async_test
async def test_stt_rate_limit_rejects_before_upload_processing(monkeypatch):
    monkeypatch.setattr(audio.audio_rate_limiter, 'is_limited', lambda key: True)
    user = SimpleNamespace(id='admin-1', role='admin')

    with pytest.raises(HTTPException) as exc_info:
        await audio.transcription(request(), upload(), user=user)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == 'Audio rate limit exceeded.'


@async_test
async def test_web_speech_fallback_uses_local_whisper(monkeypatch, tmp_path):
    async def get_config(key, default=None):
        return 'web' if key == 'audio.stt.engine' else default

    async def local_whisper(request, file_path, languages, file_dir, request_id):
        return {'text': 'local fallback'}

    monkeypatch.setattr(audio.Config, 'get', get_config)
    monkeypatch.setattr(audio, '_transcribe_whisper', local_whisper)
    file_path = tmp_path / 'sample.wav'
    file_path.write_bytes(b'RIFFxxxxWAVEdata')

    result = await audio.transcription_handler(request(), str(file_path), {'language': 'en'})

    assert result == {'text': 'local fallback'}


@async_test
async def test_audio_health_is_lazy_and_contains_no_credentials(monkeypatch):
    service = SimpleNamespace(
        readiness=lambda: {
            'loaded': False,
            'model': 'base',
            'compute_type': 'int8',
            'active_transcriptions': 0,
        }
    )

    async def get_service(_request):
        return service

    async def get_config(key, default=None):
        return {'audio.stt.engine': '', 'audio.tts.engine': 'openai'}.get(key, default)

    monkeypatch.setattr(audio, 'get_whisper_service', get_service)
    monkeypatch.setattr(audio.Config, 'get', get_config)
    monkeypatch.setattr(audio, 'ffmpeg_readiness', lambda: {'ffmpeg': True, 'ffprobe': True})
    monkeypatch.setattr(
        audio.speech_cache,
        'readiness',
        lambda: {'writable': True, 'ttl_seconds': 60, 'max_entries': 10},
    )

    result = await audio.audio_health(request(), user=SimpleNamespace(role='admin'))
    payload = result.model_dump()

    assert payload['status'] == 'ready'
    assert payload['stt_engine'] == 'whisper'
    assert payload['whisper']['loaded'] is False
    assert not {'api_key', 'token', 'secret'} & set(str(payload).lower().split())
