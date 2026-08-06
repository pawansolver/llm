"""Audio router — TTS speech synthesis and STT transcription endpoints."""

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import aiofiles
import aiohttp
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from open_webui.config import (
    AUDIO_SPEECH_CACHE_MAX_ENTRIES,
    AUDIO_SPEECH_CACHE_TTL_SECONDS,
    AUDIO_STT_MAX_CONCURRENT_REQUESTS,
    AUDIO_STT_MAX_UPLOAD_SIZE_MB,
    AUDIO_STT_MODEL_LOAD_TIMEOUT_SECONDS,
    AUDIO_STT_RATE_LIMIT,
    AUDIO_STT_RATE_LIMIT_WINDOW_SECONDS,
    AUDIO_STT_RETENTION_SECONDS,
    AUDIO_STT_TIMEOUT_SECONDS,
    CACHE_DIR,
    ELEVENLABS_API_BASE_URL,
    WHISPER_COMPUTE_TYPE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL_AUTO_UPDATE,
    WHISPER_MODEL_DIR,
    WHISPER_VAD_FILTER,
)
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    AIOHTTP_FILE_STREAM_CHUNK_SIZE,
    BYPASS_PYDUB_PREPROCESSING,
    DEVICE_TYPE,
    ENABLE_FORWARD_USER_INFO_HEADERS,
)
from open_webui.events import EVENTS, publish_event
from open_webui.models.config import Config
from open_webui.services.audio import (
    AudioCacheManager,
    AudioServiceError,
    FasterWhisperService,
    ffmpeg_readiness,
    save_validated_upload,
)
from open_webui.utils.access_control import has_permission
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.headers import include_user_info_headers
from open_webui.utils.rate_limit import RateLimiter
from open_webui.utils.redis import get_redis_client
from open_webui.utils.session_pool import get_session
from pydantic import BaseModel, Field

# pydub needs stdlib audioop (gone in 3.13); keep requires-python capped < 3.13
from pydub import AudioSegment
from pydub.utils import mediainfo

log = logging.getLogger(__name__)
router = APIRouter()

# --- Constants ---

MAX_FILE_SIZE_MB: int = 20
MAX_FILE_SIZE: int = MAX_FILE_SIZE_MB * 1024 * 1024
AZURE_MAX_FILE_SIZE_MB: int = 200
AZURE_MAX_FILE_SIZE: int = AZURE_MAX_FILE_SIZE_MB * 1024 * 1024

SPEECH_CACHE_DIR = CACHE_DIR / 'audio' / 'speech'
SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
TRANSCRIPTION_TEMP_DIR = CACHE_DIR / 'audio' / 'tmp'
TRANSCRIPTION_TEMP_DIR.mkdir(parents=True, exist_ok=True)

speech_cache = AudioCacheManager(
    SPEECH_CACHE_DIR,
    ttl_seconds=AUDIO_SPEECH_CACHE_TTL_SECONDS,
    max_entries=AUDIO_SPEECH_CACHE_MAX_ENTRIES,
)
audio_rate_limiter = RateLimiter(
    redis_client=get_redis_client(),
    limit=max(1, AUDIO_STT_RATE_LIMIT),
    window=AUDIO_STT_RATE_LIMIT_WINDOW_SECONDS,
    bucket_size=min(60, AUDIO_STT_RATE_LIMIT_WINDOW_SECONDS),
    enabled=AUDIO_STT_RATE_LIMIT > 0,
)

TTS_CONFIG_KEYS = {
    'OPENAI_API_BASE_URL': 'audio.tts.openai.api_base_url',
    'OPENAI_API_KEY': 'audio.tts.openai.api_key',
    'OPENAI_PARAMS': 'audio.tts.openai.params',
    'API_KEY': 'audio.tts.api_key',
    'ENGINE': 'audio.tts.engine',
    'MODEL': 'audio.tts.model',
    'VOICE': 'audio.tts.voice',
    'SPLIT_ON': 'audio.tts.split_on',
    'AZURE_SPEECH_REGION': 'audio.tts.azure.speech_region',
    'AZURE_SPEECH_BASE_URL': 'audio.tts.azure.speech_base_url',
    'AZURE_SPEECH_OUTPUT_FORMAT': 'audio.tts.azure.speech_output_format',
    'MISTRAL_API_KEY': 'audio.tts.mistral.api_key',
    'MISTRAL_API_BASE_URL': 'audio.tts.mistral.api_base_url',
    'CACHE_TTL_SECONDS': 'audio.tts.cache_ttl_seconds',
    'CACHE_MAX_ENTRIES': 'audio.tts.cache_max_entries',
}

STT_CONFIG_KEYS = {
    'OPENAI_API_BASE_URL': 'audio.stt.openai.api_base_url',
    'OPENAI_API_KEY': 'audio.stt.openai.api_key',
    'OPENAI_API_REQUEST_FORMAT': 'audio.stt.openai.api_request_format',
    'ENGINE': 'audio.stt.engine',
    'MODEL': 'audio.stt.model',
    'SUPPORTED_CONTENT_TYPES': 'audio.stt.supported_content_types',
    'ALLOWED_EXTENSIONS': 'audio.stt.allowed_extensions',
    'MAX_UPLOAD_SIZE_MB': 'audio.stt.max_upload_size_mb',
    'MAX_CONCURRENT_REQUESTS': 'audio.stt.max_concurrent_requests',
    'TIMEOUT_SECONDS': 'audio.stt.timeout_seconds',
    'RATE_LIMIT': 'audio.stt.rate_limit',
    'RATE_LIMIT_WINDOW_SECONDS': 'audio.stt.rate_limit_window_seconds',
    'RETENTION_SECONDS': 'audio.stt.retention_seconds',
    'WHISPER_MODEL': 'audio.stt.whisper_model',
    'DEEPGRAM_API_KEY': 'audio.stt.deepgram.api_key',
    'AZURE_API_KEY': 'audio.stt.azure.api_key',
    'AZURE_REGION': 'audio.stt.azure.region',
    'AZURE_LOCALES': 'audio.stt.azure.locales',
    'AZURE_BASE_URL': 'audio.stt.azure.base_url',
    'AZURE_MAX_SPEAKERS': 'audio.stt.azure.max_speakers',
    'MISTRAL_API_KEY': 'audio.stt.mistral.api_key',
    'MISTRAL_API_BASE_URL': 'audio.stt.mistral.api_base_url',
    'MISTRAL_USE_CHAT_COMPLETIONS': 'audio.stt.mistral.use_chat_completions',
}

MASKED_SECRET = '********'
TTS_SECRET_FIELDS = {'OPENAI_API_KEY', 'API_KEY', 'MISTRAL_API_KEY'}
STT_SECRET_FIELDS = {
    'OPENAI_API_KEY',
    'DEEPGRAM_API_KEY',
    'AZURE_API_KEY',
    'MISTRAL_API_KEY',
}


async def get_config_values(key_map: dict[str, str], secret_fields: set[str] | None = None) -> dict:
    values = await Config.get_many(*key_map.values())
    result = {field: values[storage_key] for field, storage_key in key_map.items() if storage_key in values}
    for field in secret_fields or set():
        if result.get(field):
            result[field] = MASKED_SECRET
    return result


def config_updates(data: dict, key_map: dict[str, str]) -> dict:
    return {key_map[field]: value for field, value in data.items() if field in key_map}


async def preserve_masked_secrets(data: dict, key_map: dict[str, str], secret_fields: set[str]) -> dict:
    """Replace masked placeholders with stored values before persisting an update."""
    result = dict(data)
    for field in secret_fields:
        if result.get(field) == MASKED_SECRET:
            result[field] = await Config.get(key_map[field], '')
    return result


async def cleanup_stale_transcriptions(retention_seconds: int) -> None:
    if retention_seconds <= 0:
        return
    cutoff = time.time() - retention_seconds

    def cleanup() -> None:
        for path in TRANSCRIPTION_TEMP_DIR.iterdir():
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue

    await asyncio.to_thread(cleanup)


async def create_whisper_service(
    *,
    model_name: str | None = None,
    concurrency: int | None = None,
    timeout_seconds: int | None = None,
) -> FasterWhisperService:
    return FasterWhisperService(
        model_name=model_name or await Config.get('audio.stt.whisper_model'),
        compute_type=WHISPER_COMPUTE_TYPE,
        model_dir=WHISPER_MODEL_DIR,
        device=DEVICE_TYPE if DEVICE_TYPE == 'cuda' else 'cpu',
        auto_update=WHISPER_MODEL_AUTO_UPDATE,
        concurrency=concurrency
        or int(await Config.get('audio.stt.max_concurrent_requests', AUDIO_STT_MAX_CONCURRENT_REQUESTS)),
        timeout_seconds=timeout_seconds
        or int(await Config.get('audio.stt.timeout_seconds', AUDIO_STT_TIMEOUT_SECONDS)),
        load_timeout_seconds=AUDIO_STT_MODEL_LOAD_TIMEOUT_SECONDS,
        vad_filter=WHISPER_VAD_FILTER,
    )


async def get_whisper_service(request: Request) -> FasterWhisperService:
    service = getattr(request.app.state, 'audio_whisper_service', None)
    if service is None:
        service = await create_whisper_service()
        request.app.state.audio_whisper_service = service
    return service


def is_audio_conversion_required(file_path):
    """
    Check if the given audio file needs conversion to mp3.
    """
    SUPPORTED_FORMATS = {'flac', 'm4a', 'mp3', 'mp4', 'mpeg', 'wav', 'webm'}

    if not os.path.isfile(file_path):
        log.error('Audio file not found during conversion check')
        return False

    if not ffmpeg_readiness()['ffprobe']:
        log.debug('ffprobe is unavailable; skipping codec inspection')
        return False

    try:
        info = mediainfo(file_path)
        codec_name = info.get('codec_name', '').lower()
        codec_type = info.get('codec_type', '').lower()
        codec_tag_string = info.get('codec_tag_string', '').lower()

        if codec_name == 'aac' and codec_type == 'audio' and codec_tag_string == 'mp4a':
            # File is AAC/mp4a audio, recommend mp3 conversion
            return True

        # If the codec name is in the supported formats
        if codec_name in SUPPORTED_FORMATS:
            return False

        return True
    except Exception as e:
        log.error(f'Error getting audio format: {e}')
        return False


def convert_audio_to_mp3(file_path):
    """Convert audio file to mp3 format."""
    try:
        output_path = os.path.splitext(file_path)[0] + '.mp3'
        audio = AudioSegment.from_file(file_path)
        audio.export(output_path, format='mp3')
        log.info('Converted audio to MP3')
        return output_path
    except Exception as e:
        log.error(f'Error converting audio file: {e}')
        return None


def transcode_audio_to_mp3(audio_data: bytes, content_type_header: str, output_path: str) -> bool:
    """
    Transcode audio bytes to MP3 if the Content-Type indicates a non-MP3 format.

    Handles raw PCM audio (e.g. Gemini-TTS via OpenRouter/LiteLLM) by parsing
    optional rate/channels from the Content-Type params, defaulting to 24kHz,
    16-bit, mono. For other non-MP3 formats, uses pydub auto-detection.

    Returns True if transcoding was performed, False if the data is already MP3.
    Respects BYPASS_PYDUB_PREPROCESSING — when set, writes raw bytes and logs a warning.
    """
    mime_type = content_type_header.split(';')[0].strip().lower()

    if mime_type in ('audio/mpeg', 'audio/mp3'):
        return False

    if BYPASS_PYDUB_PREPROCESSING:
        log.warning(
            f'TTS returned {mime_type} but BYPASS_PYDUB_PREPROCESSING is set; writing raw audio without transcoding'
        )
        return False

    if mime_type in ('audio/pcm', 'audio/l16', 'audio/raw'):
        # Parse optional rate/channels from Content-Type params,
        # default: 24kHz, 16-bit, mono (standard for Gemini TTS).
        ct_params = {}
        for part in content_type_header.split(';')[1:]:
            key_val = part.strip().split('=')
            if len(key_val) == 2:
                ct_params[key_val[0].strip().lower()] = key_val[1].strip()

        sample_rate = int(ct_params.get('rate', 24000))
        channels = int(ct_params.get('channels', 1))

        audio_segment = AudioSegment.from_raw(
            io.BytesIO(audio_data),
            sample_width=2,
            frame_rate=sample_rate,
            channels=channels,
        )
    else:
        audio_segment = AudioSegment.from_file(io.BytesIO(audio_data))

    audio_segment.export(str(output_path), format='mp3')
    log.info('Transcoded TTS response to MP3', extra={'content_type': mime_type})
    return True


class TTSConfigForm(BaseModel):
    OPENAI_API_BASE_URL: str
    OPENAI_API_KEY: str
    OPENAI_PARAMS: dict | None = None
    API_KEY: str
    ENGINE: str
    MODEL: str
    VOICE: str
    SPLIT_ON: str
    AZURE_SPEECH_REGION: str
    AZURE_SPEECH_BASE_URL: str
    AZURE_SPEECH_OUTPUT_FORMAT: str
    MISTRAL_API_KEY: str
    MISTRAL_API_BASE_URL: str
    CACHE_TTL_SECONDS: int = Field(default=AUDIO_SPEECH_CACHE_TTL_SECONDS, ge=0)
    CACHE_MAX_ENTRIES: int = Field(default=AUDIO_SPEECH_CACHE_MAX_ENTRIES, ge=0)


class STTConfigForm(BaseModel):
    OPENAI_API_BASE_URL: str
    OPENAI_API_KEY: str
    OPENAI_API_REQUEST_FORMAT: str = 'multipart'
    ENGINE: str
    MODEL: str
    SUPPORTED_CONTENT_TYPES: list[str] = Field(default_factory=list)
    ALLOWED_EXTENSIONS: list[str] = Field(default_factory=list)
    MAX_UPLOAD_SIZE_MB: int = Field(default=AUDIO_STT_MAX_UPLOAD_SIZE_MB, ge=1)
    MAX_CONCURRENT_REQUESTS: int = Field(default=AUDIO_STT_MAX_CONCURRENT_REQUESTS, ge=1)
    TIMEOUT_SECONDS: int = Field(default=AUDIO_STT_TIMEOUT_SECONDS, ge=1)
    RATE_LIMIT: int = Field(default=AUDIO_STT_RATE_LIMIT, ge=0)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=AUDIO_STT_RATE_LIMIT_WINDOW_SECONDS, ge=1)
    RETENTION_SECONDS: int = Field(default=AUDIO_STT_RETENTION_SECONDS, ge=0)
    WHISPER_MODEL: str
    DEEPGRAM_API_KEY: str
    AZURE_API_KEY: str
    AZURE_REGION: str
    AZURE_LOCALES: str
    AZURE_BASE_URL: str
    AZURE_MAX_SPEAKERS: str
    MISTRAL_API_KEY: str
    MISTRAL_API_BASE_URL: str
    MISTRAL_USE_CHAT_COMPLETIONS: bool


class AudioConfigUpdateForm(BaseModel):
    tts: TTSConfigForm
    stt: STTConfigForm


class TranscriptionResponse(BaseModel):
    text: str
    filename: str


class AudioHealthResponse(BaseModel):
    status: str
    stt_engine: str
    tts_engine: str
    whisper: dict
    cache: dict
    tools: dict[str, bool]


@router.get('/config')
async def get_audio_config(request: Request, user=Depends(get_admin_user)):
    return {
        'tts': await get_config_values(TTS_CONFIG_KEYS, TTS_SECRET_FIELDS),
        'stt': await get_config_values(STT_CONFIG_KEYS, STT_SECRET_FIELDS),
    }


@router.post('/config/update')
async def update_audio_config(request: Request, form_data: AudioConfigUpdateForm, user=Depends(get_admin_user)):
    tts_data = await preserve_masked_secrets(form_data.tts.model_dump(), TTS_CONFIG_KEYS, TTS_SECRET_FIELDS)
    stt_data = await preserve_masked_secrets(form_data.stt.model_dump(), STT_CONFIG_KEYS, STT_SECRET_FIELDS)
    replacement_service = None
    if form_data.stt.ENGINE in {'', 'web'}:
        replacement_service = await create_whisper_service(
            model_name=form_data.stt.WHISPER_MODEL,
            concurrency=max(1, form_data.stt.MAX_CONCURRENT_REQUESTS),
            timeout_seconds=max(1, form_data.stt.TIMEOUT_SECONDS),
        )
        try:
            await replacement_service.reload(form_data.stt.WHISPER_MODEL)
        except AudioServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    await Config.upsert(
        {
            **config_updates(tts_data, TTS_CONFIG_KEYS),
            **config_updates(stt_data, STT_CONFIG_KEYS),
        }
    )
    current_service = getattr(request.app.state, 'audio_whisper_service', None)
    if replacement_service is not None:
        request.app.state.audio_whisper_service = replacement_service
    elif current_service is not None:
        await current_service.unload()

    audio_rate_limiter.limit = max(1, form_data.stt.RATE_LIMIT)
    audio_rate_limiter.window = max(1, form_data.stt.RATE_LIMIT_WINDOW_SECONDS)
    audio_rate_limiter.bucket_size = min(60, audio_rate_limiter.window)
    audio_rate_limiter.num_buckets = max(1, audio_rate_limiter.window // audio_rate_limiter.bucket_size)
    audio_rate_limiter.enabled = form_data.stt.RATE_LIMIT > 0
    speech_cache.ttl_seconds = max(0, form_data.tts.CACHE_TTL_SECONDS)
    speech_cache.max_entries = max(0, form_data.tts.CACHE_MAX_ENTRIES)

    config = await get_audio_config(request, user)
    await publish_event(
        request,
        EVENTS.CONFIG_UPDATED,
        actor=user,
        subject_id='audio',
        data={
            'tts_engine': config.get('tts', {}).get('ENGINE'),
            'stt_engine': config.get('stt', {}).get('ENGINE'),
        },
    )
    return config


def load_speech_pipeline(request):
    from datasets import load_dataset
    from transformers import pipeline

    if request.app.state.speech_synthesiser is None:
        request.app.state.speech_synthesiser = pipeline('text-to-speech', 'microsoft/speecht5_tts')

    if request.app.state.speech_speaker_embeddings_dataset is None:
        request.app.state.speech_speaker_embeddings_dataset = load_dataset(
            'Matthijs/cmu-arctic-xvectors', split='validation'
        )


async def _raise_tts_error(exc: Exception, r=None) -> None:
    """Raise a standardised HTTPException from a TTS provider failure."""
    code = r.status if r is not None else 500
    detail = 'Open WebUI: Server Connection Error'
    if r is not None:
        try:
            res = await r.json()
            if 'error' in res:
                msg = res['error']
                detail = f'External: {msg.get("message", msg) if isinstance(msg, dict) else msg}'
            elif 'message' in res:
                detail = f'External: {res["message"]}'
        except Exception:
            detail = f'External: {exc}'
    raise HTTPException(status_code=code, detail=detail)


async def _write_tts_cache(
    file_path: Path,
    audio: bytes,
    body_path: Path,
    payload: dict,
) -> None:
    """Persist audio + request metadata to the speech cache."""
    async with aiofiles.open(file_path, 'wb') as f:
        await f.write(audio)
    async with aiofiles.open(body_path, 'w') as f:
        await f.write(json.dumps(payload))


async def _tts_openai(request, payload, file_path, file_body_path, user):
    """Generate speech via an OpenAI-compatible TTS endpoint."""
    payload['model'] = await Config.get('audio.tts.model')
    if not payload.get('voice'):
        payload['voice'] = await Config.get('audio.tts.voice')
    payload = {**payload, **(await Config.get('audio.tts.openai.params') or {})}
    api_key = await Config.get('audio.tts.openai.api_key')
    api_base_url = await Config.get('audio.tts.openai.api_base_url')

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    if ENABLE_FORWARD_USER_INFO_HEADERS:
        headers = include_user_info_headers(headers, user)

    r = None
    try:
        session = await get_session()
        r = await session.post(
            url=f'{api_base_url}/audio/speech',
            json=payload,
            headers=headers,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        )
        r.raise_for_status()

        audio_data = await r.read()
        content_type = r.headers.get('Content-Type', 'audio/mpeg')

        if not await asyncio.to_thread(transcode_audio_to_mp3, audio_data, content_type, file_path):
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(audio_data)

        async with aiofiles.open(file_body_path, 'w') as f:
            await f.write(json.dumps(payload))

        return FileResponse(file_path)
    except Exception as exc:
        log.exception(exc)
        await _raise_tts_error(exc, r)


async def _tts_elevenlabs(request, payload, file_path, file_body_path, user):
    """Generate speech via the ElevenLabs TTS API."""
    voice_id = (payload.get('voice') or '').strip()
    if not voice_id:
        raise HTTPException(status_code=400, detail='Invalid voice id')

    available_voices = await get_available_voices(request)
    if available_voices and voice_id not in available_voices:
        raise HTTPException(status_code=400, detail='Invalid voice id')

    r = None
    try:
        session = await get_session()
        async with session.post(
            f'{ELEVENLABS_API_BASE_URL}/v1/text-to-speech/{voice_id}',
            json={
                'text': payload['input'],
                'model_id': await Config.get('audio.tts.model'),
                'voice_settings': {'stability': 0.5, 'similarity_boost': 0.5},
            },
            headers={
                'Accept': 'audio/mpeg',
                'Content-Type': 'application/json',
                'xi-api-key': await Config.get('audio.tts.api_key'),
            },
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as r:
            r.raise_for_status()
            await _write_tts_cache(file_path, await r.read(), file_body_path, payload)
        return FileResponse(file_path)
    except Exception as exc:
        log.exception(exc)
        await _raise_tts_error(exc, r)


async def _tts_azure(request, payload, file_path, file_body_path, user):
    """Generate speech via Azure Cognitive Services TTS."""
    az_region = await Config.get('audio.tts.azure.speech_region') or 'eastus'
    az_base = await Config.get('audio.tts.azure.speech_base_url')
    language = payload.get('voice') or await Config.get('audio.tts.voice')
    locale = '-'.join(language.split('-')[:2])
    output_format = await Config.get('audio.tts.azure.speech_output_format')

    ssml = (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{html.escape(locale)}">'
        f'<voice name="{html.escape(language)}">{html.escape(payload["input"])}</voice>'
        f'</speak>'
    )

    r = None
    try:
        session = await get_session()
        async with session.post(
            (az_base or f'https://{az_region}.tts.speech.microsoft.com') + '/cognitiveservices/v1',
            headers={
                'Ocp-Apim-Subscription-Key': await Config.get('audio.tts.api_key'),
                'Content-Type': 'application/ssml+xml',
                'X-Microsoft-OutputFormat': output_format,
            },
            data=ssml,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as r:
            r.raise_for_status()
            await _write_tts_cache(file_path, await r.read(), file_body_path, payload)
        return FileResponse(file_path)
    except Exception as exc:
        log.exception(exc)
        await _raise_tts_error(exc, r)


async def _tts_transformers(request, payload, file_path, file_body_path, user):
    """Generate speech via the local HuggingFace SpeechT5 pipeline (thread-offloaded)."""
    import soundfile as sf
    import torch

    await asyncio.to_thread(load_speech_pipeline, request)

    embeddings = request.app.state.speech_speaker_embeddings_dataset
    model_name = await Config.get('audio.tts.model')

    idx = 6799
    try:
        idx = embeddings['filename'].index(model_name)
    except (ValueError, KeyError):
        log.debug(f'Speaker embedding not found for {model_name}, using default index {idx}')

    def _run_pipeline():
        speaker_embedding = torch.tensor(embeddings[idx]['xvector']).unsqueeze(0)
        wav = request.app.state.speech_synthesiser(
            payload['input'],  # raw text to synthesize
            forward_params={
                'speaker_embeddings': speaker_embedding,
            },
        )
        sf.write(str(file_path), wav['audio'], samplerate=wav['sampling_rate'])

    await asyncio.to_thread(_run_pipeline)

    # Audio file already written by sf.write; just persist the request metadata.
    async with aiofiles.open(file_body_path, 'w') as f:
        await f.write(json.dumps(payload))
    return FileResponse(file_path)


async def _tts_mistral(request, payload, file_path, file_body_path, user):
    """Generate speech via the Mistral TTS API."""
    api_key = await Config.get('audio.tts.mistral.api_key')
    api_base_url = await Config.get('audio.tts.mistral.api_base_url') or 'https://api.mistral.ai/v1'

    if not api_key:
        raise HTTPException(status_code=400, detail='Mistral API key is required for Mistral TTS')

    r = None
    try:
        session = await get_session()
        r = await session.post(
            url=f'{api_base_url}/audio/speech',
            json={
                'input': payload.get('input', ''),  # text to synthesize
                'model': await Config.get('audio.tts.model') or 'voxtral-mini-tts-2603',
                'voice_id': payload.get('voice', ''),
                'response_format': 'mp3',
            },
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            },
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        )
        r.raise_for_status()

        res = await r.json()
        audio_b64 = res.get('audio_data', '')
        if not audio_b64:
            raise ValueError('No audio_data in Mistral TTS response')

        await _write_tts_cache(file_path, base64.b64decode(audio_b64), file_body_path, payload)
        return FileResponse(file_path)
    except Exception as exc:
        log.exception(exc)
        await _raise_tts_error(exc, r)


# Dispatcher map: engine name -> handler
_TTS_ENGINES = {
    'openai': _tts_openai,
    'elevenlabs': _tts_elevenlabs,
    'azure': _tts_azure,
    'transformers': _tts_transformers,
    'mistral': _tts_mistral,
}


@router.post('/speech')
async def speech(request: Request, user=Depends(get_verified_user)):
    await speech_cache.cleanup()
    engine = await Config.get('audio.tts.engine')
    if engine == '':
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if user.role != 'admin' and not await has_permission(user.id, 'chat.tts', await Config.get('user.permissions')):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    body = await request.body()
    name = hashlib.sha256(
        body + str(engine).encode('utf-8') + str(await Config.get('audio.tts.model')).encode('utf-8')
    ).hexdigest()

    file_path = SPEECH_CACHE_DIR.joinpath(f'{name}.mp3')
    file_body_path = SPEECH_CACHE_DIR.joinpath(f'{name}.json')

    # Return cached result if available
    if file_path.is_file():
        await publish_event(
            request,
            EVENTS.AUDIO_SPEECH_REQUESTED,
            actor=user,
            subject_id=name,
            data={'engine': engine, 'cached': True},
        )
        return FileResponse(file_path)

    try:
        payload = json.loads(body)
    except Exception as exc:
        log.exception(exc)
        raise HTTPException(status_code=400, detail='Invalid JSON payload')

    handler = _TTS_ENGINES.get(engine)
    if handler is None:
        raise HTTPException(status_code=400, detail=f'Unsupported TTS engine: {engine}')

    response = await handler(request, payload, file_path, file_body_path, user)
    await speech_cache.cleanup()
    await publish_event(
        request,
        EVENTS.AUDIO_SPEECH_REQUESTED,
        actor=user,
        subject_id=name,
        data={
            'engine': engine,
            'model': payload.get('model'),
            'cached': False,
        },
    )
    return response


async def _transcribe_whisper(request, file_path, languages, file_dir, id):
    service = await get_whisper_service(request)
    configured_model = await Config.get('audio.stt.whisper_model')
    if service.model_name != configured_model:
        service.model_name = configured_model
    try:
        result = await service.transcribe(file_path, languages[0], cleanup_directory=file_dir)
    except AudioServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    data = {'text': result.text}

    async with aiofiles.open(os.path.join(file_dir, f'{id}.json'), 'w') as f:
        await f.write(json.dumps(data))

    return data


async def _transcribe_openai(request, file_path, filename, languages, file_dir, id, user=None):
    """Transcribe audio via an OpenAI-compatible STT endpoint."""
    r = None
    try:
        session = await get_session()
        api_key = await Config.get('audio.stt.openai.api_key')
        api_base_url = await Config.get('audio.stt.openai.api_base_url')
        request_format = (await Config.get('audio.stt.openai.api_request_format') or 'multipart').lower()

        headers = {'Authorization': f'Bearer {api_key}'}
        if user and ENABLE_FORWARD_USER_INFO_HEADERS:
            headers = include_user_info_headers(headers, user)

        for language in languages:
            payload = {'model': await Config.get('audio.stt.model')}
            if language:
                payload['language'] = language

            if request_format == 'json':
                ext = os.path.splitext(filename)[1].lower().lstrip('.') or 'wav'
                async with aiofiles.open(file_path, 'rb') as f:
                    payload['input_audio'] = {
                        'data': base64.b64encode(await f.read()).decode('utf-8'),
                        'format': 'ogg' if ext == 'oga' else ext,
                    }

                r = await session.post(
                    url=f'{api_base_url}/audio/transcriptions',
                    headers={**headers, 'Content-Type': 'application/json'},
                    json=payload,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                )
            else:
                form_data = aiohttp.FormData()
                for key, value in payload.items():
                    form_data.add_field(key, str(value))

                async def audio_chunks():
                    async with aiofiles.open(file_path, 'rb') as audio_file:
                        while chunk := await audio_file.read(AIOHTTP_FILE_STREAM_CHUNK_SIZE):
                            yield chunk

                form_data.add_field('file', audio_chunks(), filename=filename)

                r = await session.post(
                    url=f'{api_base_url}/audio/transcriptions',
                    headers=headers,
                    data=form_data,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                )
            if r.status == 200:
                break

        r.raise_for_status()
        data = await r.json()

        async with aiofiles.open(os.path.join(file_dir, f'{id}.json'), 'w') as f:
            await f.write(json.dumps(data))
        return data
    except Exception as e:
        log.exception(e)
        detail = None
        if r is not None:
            try:
                res = await r.json()
                if 'error' in res:
                    detail = f'External: {res["error"].get("message", "")}'
            except Exception:
                detail = f'External: {e}'
        raise Exception(detail if detail else 'Open WebUI: Server Connection Error')


async def _transcribe_deepgram(request, file_path, languages, file_dir, id):
    """Transcribe audio via the Deepgram listen API with language fallback."""
    content_type = mimetypes.guess_type(file_path)[0] or 'audio/wav'

    async with aiofiles.open(file_path, 'rb') as f:
        audio_bytes = await f.read()

    api_key = await Config.get('audio.stt.deepgram.api_key')
    stt_model = await Config.get('audio.stt.model')

    r = None
    try:
        session = await get_session()
        for lang in languages:
            query: dict = {'smart_format': 'true'}
            if stt_model:
                query['model'] = stt_model
            if lang:
                query['language'] = lang

            r = await session.post(
                'https://api.deepgram.com/v1/listen',
                headers={'Authorization': f'Token {api_key}', 'Content-Type': content_type},
                params=query,
                data=audio_bytes,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            )
            if r.status == 200:
                break

        r.raise_for_status()
        body = await r.json()

        # Parse the Deepgram response structure
        try:
            transcript = body['results']['channels'][0]['alternatives'][0].get('transcript', '').strip()
        except (KeyError, IndexError) as exc:
            log.error(f'Malformed Deepgram response: {exc}')
            raise Exception('Failed to parse Deepgram response') from exc

        data = {'text': transcript}
        async with aiofiles.open(os.path.join(file_dir, f'{id}.json'), 'w') as f:
            await f.write(json.dumps(data))
        return data

    except Exception as e:
        log.exception(e)
        detail = 'Open WebUI: Server Connection Error'
        if r is not None:
            try:
                res = await r.json()
                msg = (
                    res.get('error', {}).get('message', '')
                    if isinstance(res.get('error'), dict)
                    else str(res.get('error', ''))
                )
                if msg:
                    detail = f'External: {msg}'
            except Exception:
                detail = f'External: {e}'
        raise Exception(detail)


async def _transcribe_azure(request, file_path, filename, file_dir, id):
    """Transcribe audio via Azure Cognitive Services batch transcription."""
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=400, detail='Audio file not found')

    audio_size = os.path.getsize(file_path)
    if audio_size > AZURE_MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'File size ({audio_size // (1024 * 1024)}MB) exceeds Azure limit of {AZURE_MAX_FILE_SIZE_MB}MB',
        )

    api_key = await Config.get('audio.stt.azure.api_key')
    region = await Config.get('audio.stt.azure.region') or 'eastus'
    locale_str = await Config.get('audio.stt.azure.locales')
    base_url = await Config.get('audio.stt.azure.base_url')
    max_speakers = await Config.get('audio.stt.azure.max_speakers') or 3

    # Default to a broad set of locales when none are configured
    if len(locale_str) < 2:
        locale_str = ','.join(
            [
                'en-US',
                'es-ES',
                'es-MX',
                'fr-FR',
                'hi-IN',
                'it-IT',
                'de-DE',
                'en-GB',
                'en-IN',
                'ja-JP',
                'ko-KR',
                'pt-BR',
                'zh-CN',
            ]
        )

    if not api_key or not region:
        raise HTTPException(status_code=400, detail='Azure API key and region are required for Azure STT')

    # Build the transcription definition payload
    definition = json.dumps(
        {'locales': locale_str.split(','), 'diarization': {'maxSpeakers': max_speakers, 'enabled': True}}
        if locale_str
        else {}
    )
    endpoint = (
        base_url or f'https://{region}.api.cognitive.microsoft.com'
    ) + '/speechtotext/transcriptions:transcribe?api-version=2024-11-15'

    r = None
    try:
        session = await get_session()
        form_data = aiohttp.FormData()
        form_data.add_field('definition', definition)

        async def audio_chunks():
            async with aiofiles.open(file_path, 'rb') as audio_file:
                while chunk := await audio_file.read(AIOHTTP_FILE_STREAM_CHUNK_SIZE):
                    yield chunk

        form_data.add_field('audio', audio_chunks(), filename=filename)
        r = await session.post(
            url=endpoint,
            data=form_data,
            headers={'Ocp-Apim-Subscription-Key': api_key},
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        )
        r.raise_for_status()
        response = await r.json()

        if not response.get('combinedPhrases'):
            raise ValueError('No transcription found in response')

        transcript = response['combinedPhrases'][0].get('text', '').strip()
        if not transcript:
            raise ValueError('Empty transcript in response')

        data = {'text': transcript}

        async with aiofiles.open(os.path.join(file_dir, f'{id}.json'), 'w') as f:
            await f.write(json.dumps(data))

        return data

    except (KeyError, IndexError, ValueError) as e:
        log.exception('Error parsing Azure response')
        raise HTTPException(status_code=500, detail=f'Failed to parse Azure response: {str(e)}')
    except aiohttp.ClientResponseError as e:
        log.exception(e)
        detail = None
        try:
            if r is not None and r.status != 200:
                res = await r.json()
                if 'code' in res and 'message' in res:
                    azure_code = res.get('innerError', {}).get('code', res['code'])
                    user_facing_codes = {
                        'EmptyAudioFile',
                        'AudioLengthLimitExceeded',
                        'NoLanguageIdentified',
                        'MultipleLanguagesIdentified',
                    }
                    if azure_code in user_facing_codes:
                        detail = res['message']
                    else:
                        log.error(f'Azure STT error [{azure_code}]: {res["message"]}')
                        detail = 'An error occurred during transcription.'
                elif 'error' in res:
                    detail = f'External: {res["error"].get("message", "")}'
        except Exception:
            detail = f'External: {e}'
        raise HTTPException(
            status_code=e.status if e.status else 500,
            detail=detail if detail else 'Open WebUI: Server Connection Error',
        )


async def transcription_handler(request, file_path, metadata, user=None):
    filename = os.path.basename(file_path)
    file_dir = os.path.dirname(file_path)
    id = filename.split('.')[0]

    metadata = metadata or {}

    languages = [
        metadata.get('language', None) if not WHISPER_LANGUAGE else WHISPER_LANGUAGE,
        None,  # Always fallback to None in case transcription fails
    ]

    if await Config.get('audio.stt.engine') in {'', 'web'}:
        return await _transcribe_whisper(request, file_path, languages, file_dir, id)
    elif await Config.get('audio.stt.engine') == 'openai':
        return await _transcribe_openai(request, file_path, filename, languages, file_dir, id, user)
    elif await Config.get('audio.stt.engine') == 'deepgram':
        return await _transcribe_deepgram(request, file_path, languages, file_dir, id)
    elif await Config.get('audio.stt.engine') == 'azure':
        return await _transcribe_azure(request, file_path, filename, file_dir, id)

    elif await Config.get('audio.stt.engine') == 'mistral':
        return await _transcribe_mistral(request, file_path, filename, metadata, file_dir, id)


async def _transcribe_mistral(request, file_path, filename, metadata, file_dir, id):
    """Transcribe audio via the Mistral STT API."""
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=400, detail='Audio file not found')

    file_size = os.path.getsize(file_path)
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f'File size exceeds limit of {MAX_FILE_SIZE_MB}MB')

    api_key = await Config.get('audio.stt.mistral.api_key')
    api_base_url = await Config.get('audio.stt.mistral.api_base_url') or 'https://api.mistral.ai/v1'
    use_chat_completions = await Config.get('audio.stt.mistral.use_chat_completions')

    if not api_key:
        raise HTTPException(status_code=400, detail='Mistral API key is required for Mistral STT')

    r = None
    try:
        model = await Config.get('audio.stt.model') or 'voxtral-mini-latest'
        log.info(
            f'Mistral STT - model: {model}, method: {"chat_completions" if use_chat_completions else "transcriptions"}'
        )

        session = await get_session()
        if use_chat_completions:
            audio_file_to_use = file_path
            if is_audio_conversion_required(file_path):
                log.debug('Converting audio to mp3 for chat completions API')
                converted_path = await asyncio.to_thread(convert_audio_to_mp3, file_path)
                if converted_path:
                    audio_file_to_use = converted_path
                else:
                    log.error('Audio conversion failed')
                    raise HTTPException(
                        status_code=500,
                        detail='Audio conversion failed. Chat completions API requires mp3 or wav format.',
                    )

            async with aiofiles.open(audio_file_to_use, 'rb') as audio_file:
                raw = await audio_file.read()
                audio_base64 = {
                    'data': base64.b64encode(raw).decode('utf-8'),
                    'format': mimetypes.guess_extension(mimetypes.guess_type(audio_file_to_use)[0]).lstrip('.'),
                }

            language = metadata.get('language', None) if metadata else None
            text_instruction = (
                f'Transcribe this audio exactly as spoken in {language}. Do not translate it.'
                if language
                else (
                    'Transcribe this audio exactly as spoken in its original language. '
                    'Do not translate it to another language.'
                )
            )

            payload = {
                'model': model,
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {'type': 'input_audio', 'input_audio': audio_base64},
                            {'type': 'text', 'text': text_instruction},
                        ],
                    }
                ],
            }

            r = await session.post(
                url=f'{api_base_url}/chat/completions',
                json=payload,
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            )
            r.raise_for_status()
            response = await r.json()

            transcript = response.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            if not transcript:
                raise ValueError('Empty transcript in response')
            data = {'text': transcript}

        else:
            mime_type, _ = mimetypes.guess_type(file_path)
            if not mime_type:
                mime_type = 'audio/webm'

            form_data = aiohttp.FormData()
            form_data.add_field('model', model)

            language = metadata.get('language', None) if metadata else None
            if language:
                form_data.add_field('language', language)

            async def audio_chunks():
                async with aiofiles.open(file_path, 'rb') as audio_file:
                    while chunk := await audio_file.read(AIOHTTP_FILE_STREAM_CHUNK_SIZE):
                        yield chunk

            form_data.add_field('file', audio_chunks(), filename=filename, content_type=mime_type)

            r = await session.post(
                url=f'{api_base_url}/audio/transcriptions',
                data=form_data,
                headers={'Authorization': f'Bearer {api_key}'},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            )
            r.raise_for_status()
            response = await r.json()

            transcript = response.get('text', '').strip()
            if not transcript:
                raise ValueError('Empty transcript in response')
            data = {'text': transcript}

        async with aiofiles.open(os.path.join(file_dir, f'{id}.json'), 'w') as f:
            await f.write(json.dumps(data))

        return data

    except ValueError as e:
        log.exception('Error parsing Mistral response')
        raise HTTPException(status_code=500, detail=f'Failed to parse Mistral response: {str(e)}')
    except aiohttp.ClientResponseError as e:
        log.exception(e)
        detail = None
        try:
            if r is not None and r.status != 200:
                res = await r.json()
                if 'error' in res:
                    detail = f'External: {res["error"].get("message", "")}'
                else:
                    detail = f'External: {await r.text()}'
        except Exception:
            detail = f'External: {e}'
        raise HTTPException(
            status_code=e.status if e.status else 500,
            detail=detail if detail else 'Open WebUI: Server Connection Error',
        )


async def transcribe(request: Request, file_path: str, metadata: dict | None = None, user=None):
    if BYPASS_PYDUB_PREPROCESSING:
        log.info('Bypassing pydub preprocessing (BYPASS_PYDUB_PREPROCESSING=true)')
        chunk_paths = [file_path]
    else:
        if is_audio_conversion_required(file_path):
            file_path = await asyncio.to_thread(convert_audio_to_mp3, file_path)
            if not file_path:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Audio conversion failed. The audio file may be corrupted or empty.',
                )

        try:
            file_path = await asyncio.to_thread(compress_audio, file_path)
        except Exception as e:
            log.exception(e)

        # Always produce a list of chunk paths (could be one entry if small)
        try:
            chunk_paths = await asyncio.to_thread(split_audio, file_path, MAX_FILE_SIZE)
        except Exception as e:
            log.exception(e)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(e, 'Error processing audio file'),
            )

    try:
        if await Config.get('audio.stt.engine') in {'', 'web'}:
            # Keep chunks from one upload sequential; cross-request concurrency is
            # bounded by FasterWhisperService and a timeout can then clean safely.
            results = [await transcription_handler(request, chunk_path, metadata, user) for chunk_path in chunk_paths]
        else:
            tasks = [transcription_handler(request, chunk_path, metadata, user) for chunk_path in chunk_paths]
            # gather keeps results in chunk order, unlike as_completed
            results = await asyncio.gather(*tasks)
    except HTTPException:
        raise
    except Exception as transcribe_exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Error transcribing chunk: {transcribe_exc}',
        )
    finally:
        # Clean up only the temporary chunks, never the original file
        for chunk_path in chunk_paths:
            if chunk_path != file_path and os.path.isfile(chunk_path):
                try:
                    await asyncio.to_thread(os.remove, chunk_path)
                except Exception:
                    pass

    return {
        'text': ' '.join([result['text'] for result in results]),
    }


def compress_audio(file_path):
    if os.path.getsize(file_path) > MAX_FILE_SIZE:
        id = os.path.splitext(os.path.basename(file_path))[0]  # Handles names with multiple dots
        file_dir = os.path.dirname(file_path)

        audio = AudioSegment.from_file(file_path)
        audio = audio.set_frame_rate(16000).set_channels(1)  # Compress audio

        compressed_path = os.path.join(file_dir, f'{id}_compressed.mp3')
        audio.export(compressed_path, format='mp3', bitrate='32k')
        # log.debug(f"Compressed audio to {compressed_path}")  # Uncomment if log is defined

        return compressed_path
    else:
        return file_path


def split_audio(file_path, max_bytes, format='mp3', bitrate='32k'):
    """
    Splits audio into chunks not exceeding max_bytes.
    Returns a list of chunk file paths. If audio fits, returns list with original path.
    """
    file_size = os.path.getsize(file_path)
    if file_size <= max_bytes:
        return [file_path]  # Nothing to split

    audio = AudioSegment.from_file(file_path)
    duration_ms = len(audio)
    orig_size = file_size

    approx_chunk_ms = max(int(duration_ms * (max_bytes / orig_size)) - 1000, 1000)
    chunks = []
    start = 0
    i = 0

    base, _ = os.path.splitext(file_path)

    while start < duration_ms:
        end = min(start + approx_chunk_ms, duration_ms)
        chunk = audio[start:end]
        chunk_path = f'{base}_chunk_{i}.{format}'
        chunk.export(chunk_path, format=format, bitrate=bitrate)

        # Reduce chunk duration if still too large
        while os.path.getsize(chunk_path) > max_bytes and (end - start) > 5000:
            end = start + ((end - start) // 2)
            chunk = audio[start:end]
            chunk.export(chunk_path, format=format, bitrate=bitrate)

        if os.path.getsize(chunk_path) > max_bytes:
            os.remove(chunk_path)
            raise Exception('Audio chunk cannot be reduced below max file size.')

        chunks.append(chunk_path)
        start = end
        i += 1

    return chunks


@router.post('/transcriptions')
async def transcription(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(None),
    user=Depends(get_verified_user),
) -> TranscriptionResponse:
    if user.role != 'admin' and not await has_permission(user.id, 'chat.stt', await Config.get('user.permissions')):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    request_id = request.headers.get('x-request-id') or str(uuid.uuid4())
    client_ip = request.client.host if request.client else 'unknown'
    if audio_rate_limiter.is_limited(f'audio-stt:{user.id}:{client_ip}'):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Audio rate limit exceeded.')

    retention_seconds = int(await Config.get('audio.stt.retention_seconds', AUDIO_STT_RETENTION_SECONDS))
    await cleanup_stale_transcriptions(retention_seconds)
    max_size_mb = int(await Config.get('audio.stt.max_upload_size_mb', AUDIO_STT_MAX_UPLOAD_SIZE_MB))
    max_bytes = max(1, max_size_mb) * 1024 * 1024
    content_length = request.headers.get('content-length')
    if content_length:
        try:
            # Multipart framing is not part of the file limit; allow bounded framing overhead.
            if int(content_length) > max_bytes + 1024 * 1024:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail='Audio upload exceeds the configured size limit.',
                )
        except ValueError:
            raise HTTPException(status_code=400, detail='Invalid Content-Length header.')

    safe_name = os.path.basename(file.filename or '')
    extension = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    allowed_extensions = {str(ext).lower().lstrip('.') for ext in await Config.get('audio.stt.allowed_extensions', [])}
    supported_content_types = await Config.get('audio.stt.supported_content_types', [])
    started = time.monotonic()
    engine = await Config.get('audio.stt.engine')
    size = 0
    temp_dir = tempfile.mkdtemp(prefix='stt-', dir=TRANSCRIPTION_TEMP_DIR)

    try:
        file_path = Path(temp_dir) / f'{uuid.uuid4().hex}.{extension}'
        size = await save_validated_upload(
            file,
            file_path,
            max_bytes=max_bytes,
            allowed_extensions=allowed_extensions,
            supported_content_types=supported_content_types,
        )
        if await request.is_disconnected():
            raise asyncio.CancelledError()

        metadata = {'language': language} if language else None
        result = await transcribe(request, str(file_path), metadata, user)
        await publish_event(
            request,
            EVENTS.AUDIO_TRANSCRIPTION_REQUESTED,
            actor=user,
            subject_id=request_id,
            data={
                'engine': engine or 'whisper',
                'content_type': file.content_type,
                'language_hint': language,
                'size_bytes': size,
            },
        )
        log.info(
            'audio_stt_request_completed',
            extra={
                'request_id': request_id,
                'engine': engine or 'whisper',
                'duration_ms': round((time.monotonic() - started) * 1000, 2),
                'size_bytes': size,
                'status': 200,
            },
        )
        return TranscriptionResponse(text=result['text'], filename=file_path.name)
    except AudioServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except HTTPException:
        raise
    except asyncio.CancelledError:
        log.info(
            'audio_stt_request_cancelled',
            extra={'request_id': request_id, 'engine': engine or 'whisper', 'size_bytes': size},
        )
        raise
    except Exception as exc:
        log.exception(
            'audio_stt_request_failed',
            extra={'request_id': request_id, 'engine': engine or 'whisper', 'size_bytes': size},
        )
        raise HTTPException(status_code=400, detail='Transcription failed.') from exc
    finally:
        # Native Whisper work cannot be force-cancelled. This best-effort pass is
        # repeated by the service callback after a timed-out worker releases the file.
        if retention_seconds <= 0:
            await asyncio.to_thread(shutil.rmtree, temp_dir, True)


@router.get('/health')
async def audio_health(request: Request, user=Depends(get_admin_user)) -> AudioHealthResponse:
    """Report audio dependencies without loading a local model or returning credentials."""
    service = await get_whisper_service(request)
    stt_engine = await Config.get('audio.stt.engine')
    tts_engine = await Config.get('audio.tts.engine')
    tools = ffmpeg_readiness()
    cache = speech_cache.readiness()
    whisper = service.readiness()
    ready = cache['writable'] and (stt_engine != '' or bool(tools['ffmpeg']))
    return AudioHealthResponse(
        status='ready' if ready else 'degraded',
        stt_engine=stt_engine or 'whisper',
        tts_engine=tts_engine or 'browser',
        whisper=whisper,
        cache=cache,
        tools=tools,
    )


async def get_available_models(request: Request) -> list[dict]:
    """Return the list of available TTS models for the configured engine."""
    available_models = []
    engine = await Config.get('audio.tts.engine')
    _timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)

    if engine == 'openai':
        base_url = await Config.get('audio.tts.openai.api_base_url')
        if not base_url.startswith('https://api.openai.com'):
            session = await get_session()
            try:
                async with session.get(
                    f'{base_url}/audio/models',
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                    timeout=_timeout,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    available_models = data.get('models', [])
            except Exception as e:
                log.debug(f'/audio/models not available, trying /models fallback: {e}')
                try:
                    async with session.get(
                        f'{base_url}/models',
                        ssl=AIOHTTP_CLIENT_SESSION_SSL,
                        timeout=_timeout,
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                        available_models = data.get('data', data.get('models', []))
                except Exception as e2:
                    log.error(f'Error fetching models from custom endpoint: {e2}')
                    available_models = [{'id': 'tts-1'}, {'id': 'tts-1-hd'}]
        else:
            available_models = [{'id': 'tts-1'}, {'id': 'tts-1-hd'}]

    elif engine == 'elevenlabs':
        try:
            session = await get_session()
            async with session.get(
                f'{ELEVENLABS_API_BASE_URL}/v1/models',
                headers={
                    'xi-api-key': await Config.get('audio.tts.api_key'),
                    'Content-Type': 'application/json',
                },
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                timeout=_timeout,
            ) as resp:
                resp.raise_for_status()
                models = await resp.json()
                available_models = [{'name': m['name'], 'id': m['model_id']} for m in models]
        except Exception as e:
            log.error(f'Error fetching models: {e}')

    elif engine == 'mistral':
        available_models = [{'id': 'voxtral-mini-tts-2603'}]

    return available_models


@router.get('/models')
async def get_models(request: Request, user=Depends(get_verified_user)):
    if user.role != 'admin' and not await has_permission(user.id, 'chat.tts', await Config.get('user.permissions')):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return {'models': await get_available_models(request)}


_OPENAI_DEFAULT_VOICES = {
    'alloy': 'alloy',
    'echo': 'echo',
    'fable': 'fable',
    'onyx': 'onyx',
    'nova': 'nova',
    'shimmer': 'shimmer',
}


async def get_available_voices(request) -> dict:
    """Return ``{voice_id: voice_name}`` for the configured TTS engine."""
    engine = await Config.get('audio.tts.engine')
    _timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)

    if engine == 'openai':
        base_url = await Config.get('audio.tts.openai.api_base_url')
        if not base_url.startswith('https://api.openai.com'):
            try:
                session = await get_session()
                async with session.get(
                    f'{base_url}/audio/voices',
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                    timeout=_timeout,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return {v['id']: v['name'] for v in data.get('voices', [])}
            except Exception as e:
                log.error(f'Error fetching voices from custom endpoint: {e}')
                return dict(_OPENAI_DEFAULT_VOICES)
        return dict(_OPENAI_DEFAULT_VOICES)

    if engine == 'elevenlabs':
        try:
            session = await get_session()
            async with session.get(
                f'{ELEVENLABS_API_BASE_URL}/v1/voices',
                headers={
                    'xi-api-key': await Config.get('audio.tts.api_key'),
                    'Content-Type': 'application/json',
                },
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                timeout=_timeout,
            ) as resp:
                resp.raise_for_status()
                voices_data = await resp.json()
                return {v['voice_id']: v['name'] for v in voices_data.get('voices', [])}
        except Exception as e:
            log.warning(f'Error fetching ElevenLabs voices: {e}')
            return {}

    if engine == 'azure':
        try:
            region = await Config.get('audio.tts.azure.speech_region')
            base_url = await Config.get('audio.tts.azure.speech_base_url')
            url = (base_url or f'https://{region}.tts.speech.microsoft.com') + '/cognitiveservices/voices/list'

            session = await get_session()
            async with session.get(
                url,
                headers={'Ocp-Apim-Subscription-Key': await Config.get('audio.tts.api_key')},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                timeout=_timeout,
            ) as resp:
                resp.raise_for_status()
                voices = await resp.json()
                return {v['ShortName']: f'{v["DisplayName"]} ({v["ShortName"]})' for v in voices}
        except Exception as e:
            log.error(f'Error fetching Azure voices: {e}')
            return {}

    if engine == 'mistral':
        api_key = await Config.get('audio.tts.mistral.api_key')
        api_base_url = await Config.get('audio.tts.mistral.api_base_url') or 'https://api.mistral.ai/v1'
        if api_key:
            try:
                session = await get_session()
                async with session.get(
                    f'{api_base_url}/audio/voices',
                    headers={'Authorization': f'Bearer {api_key}'},
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                    timeout=_timeout,
                ) as resp:
                    resp.raise_for_status()
                    voices_data = await resp.json()
                    items = voices_data.get('items', []) if isinstance(voices_data, dict) else voices_data
                    result = {}
                    for v in items:
                        if isinstance(v, dict):
                            vid = v.get('voice_id', v.get('id', ''))
                            if vid:
                                result[vid] = v.get('name', vid)
                    return result
            except Exception as e:
                log.error(f'Error fetching Mistral voices: {e}')

    return {}


@router.get('/voices')
async def get_voices(request: Request, user=Depends(get_verified_user)):
    if user.role != 'admin' and not await has_permission(user.id, 'chat.tts', await Config.get('user.permissions')):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return {'voices': [{'id': k, 'name': v} for k, v in (await get_available_voices(request)).items()]}
