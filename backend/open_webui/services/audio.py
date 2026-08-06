"""Hardened lifecycle, upload, cache, and readiness helpers for audio APIs."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from opentelemetry import metrics

log = logging.getLogger(__name__)

UPLOAD_CHUNK_SIZE = 1024 * 1024
EXTENSION_CONTENT_TYPES = {
    'flac': {'audio/flac', 'audio/x-flac'},
    'm4a': {'audio/mp4', 'audio/x-m4a', 'video/mp4'},
    'mp3': {'audio/mpeg', 'audio/mp3'},
    'mp4': {'audio/mp4', 'video/mp4'},
    'mpga': {'audio/mpeg'},
    'mpeg': {'audio/mpeg'},
    'oga': {'audio/ogg', 'application/ogg'},
    'ogg': {'audio/ogg', 'application/ogg'},
    'wav': {'audio/wav', 'audio/wave', 'audio/x-wav'},
    'webm': {'audio/webm', 'video/webm'},
}
_meter = metrics.get_meter(__name__)
_stt_requests = _meter.create_counter('audio.stt.requests', unit='1')
_stt_duration = _meter.create_histogram('audio.stt.duration', unit='ms')
_stt_queue_duration = _meter.create_histogram('audio.stt.queue.duration', unit='ms')


def _ffmpeg_executable() -> str | None:
    """Prefer a system ffmpeg and fall back to imageio-ffmpeg's bundled binary."""
    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        return bundled_ffmpeg if Path(bundled_ffmpeg).is_file() else None
    except (ImportError, RuntimeError, OSError):
        log.warning('No usable ffmpeg executable was found')
        return None


def configure_pydub_audio() -> dict[str, bool]:
    """Configure pydub without changing PATH or exposing executable locations."""
    ffmpeg_executable = _ffmpeg_executable()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            'ignore',
            message="Couldn't find ffmpeg or avconv",
            category=RuntimeWarning,
        )
        from pydub import AudioSegment

    if ffmpeg_executable:
        AudioSegment.converter = ffmpeg_executable
    return {
        'ffmpeg': ffmpeg_executable is not None,
        'ffprobe': shutil.which('ffprobe') is not None,
    }


configure_pydub_audio()


class AudioServiceError(RuntimeError):
    """Base error with a stable HTTP status and public detail."""

    status_code = 500
    detail = 'Audio processing failed.'


class AudioBusyError(AudioServiceError):
    status_code = 503
    detail = 'Audio transcription service is busy.'


class AudioTimeoutError(AudioServiceError):
    status_code = 503
    detail = 'Audio transcription timed out.'


class AudioModelError(AudioServiceError):
    status_code = 503
    detail = 'Local transcription model is unavailable.'


class AudioUploadTooLargeError(AudioServiceError):
    status_code = 413
    detail = 'Audio upload exceeds the configured size limit.'


class AudioUnsupportedMediaError(AudioServiceError):
    status_code = 415
    detail = 'Unsupported audio media type.'


@dataclass(frozen=True)
class WhisperResult:
    text: str
    language: str | None
    language_probability: float | None


class FasterWhisperService:
    """Lazy faster-whisper model lifecycle with serialized loads and bounded work."""

    def __init__(
        self,
        *,
        model_name: str,
        compute_type: str,
        model_dir: str,
        device: str,
        auto_update: bool,
        concurrency: int,
        timeout_seconds: float,
        load_timeout_seconds: float,
        vad_filter: bool,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self.model_dir = model_dir
        self.device = device
        self.auto_update = auto_update
        self.timeout_seconds = timeout_seconds
        self.load_timeout_seconds = load_timeout_seconds
        self.vad_filter = vad_filter
        self._model_factory = model_factory
        self._model: Any | None = None
        self._loaded_model_name: str | None = None
        self._load_task: asyncio.Task[Any] | None = None
        self._load_task_model_name: str | None = None
        self._load_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._active = 0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def active(self) -> int:
        return self._active

    def _create_model(self, model_name: str) -> Any:
        factory = self._model_factory
        if factory is None:
            from faster_whisper import WhisperModel

            factory = WhisperModel

        kwargs = {
            'model_size_or_path': model_name,
            'device': self.device,
            'compute_type': self.compute_type,
            'download_root': self.model_dir,
            'local_files_only': not self.auto_update,
        }
        return factory(**kwargs)

    async def _load(self, model_name: str) -> Any:
        if self._load_task is not None and self._load_task_model_name != model_name:
            try:
                await asyncio.wait_for(asyncio.shield(self._load_task), timeout=self.load_timeout_seconds)
            except TimeoutError as exc:
                raise AudioTimeoutError() from exc
            except Exception:
                pass
            finally:
                if self._load_task.done():
                    self._load_task = None
                    self._load_task_model_name = None

        if self._load_task is None:
            self._load_task = asyncio.create_task(asyncio.to_thread(self._create_model, model_name))
            self._load_task.add_done_callback(self._observe_background_task)
            self._load_task_model_name = model_name
        load_task = self._load_task
        try:
            model = await asyncio.wait_for(asyncio.shield(load_task), timeout=self.load_timeout_seconds)
        except TimeoutError as exc:
            raise AudioTimeoutError() from exc
        except AudioServiceError:
            raise
        except Exception as exc:
            self._load_task = None
            self._load_task_model_name = None
            log.exception('Local Whisper model load failed')
            raise AudioModelError() from exc
        self._load_task = None
        self._load_task_model_name = None
        return model

    async def get_model(self) -> Any:
        if self._model is not None and self._loaded_model_name == self.model_name:
            return self._model
        async with self._load_lock:
            if self._model is None or self._loaded_model_name != self.model_name:
                model = await self._load(self.model_name)
                self._model = model
                self._loaded_model_name = self.model_name
        return self._model

    async def reload(self, model_name: str) -> None:
        """Load a replacement before swapping, preserving a working old model on failure."""
        async with self._load_lock:
            if self._model is not None and self._loaded_model_name == model_name:
                self.model_name = model_name
                return
            replacement = await self._load(model_name)
            self._model = replacement
            self._loaded_model_name = model_name
            self.model_name = model_name

    async def unload(self) -> None:
        async with self._load_lock:
            self._model = None
            self._loaded_model_name = None

    async def transcribe(
        self,
        file_path: str,
        language: str | None = None,
        cleanup_directory: str | None = None,
    ) -> WhisperResult:
        queue_started = time.monotonic()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            self._record_transcription(queue_started, 'busy')
            raise AudioBusyError() from exc

        queue_ms = (time.monotonic() - queue_started) * 1000
        _stt_queue_duration.record(queue_ms, {'engine': 'whisper'})
        self._active += 1
        release_on_exit = True
        try:
            model = await self.get_model()

            def run() -> WhisperResult:
                kwargs: dict[str, Any] = {
                    'beam_size': 5,
                    'vad_filter': self.vad_filter,
                }
                if language:
                    kwargs['language'] = language
                # faster-whisper does not accept a ``multilingual`` argument.
                segments, info = model.transcribe(file_path, **kwargs)
                text = ''.join(segment.text for segment in segments).strip()
                return WhisperResult(
                    text=text,
                    language=getattr(info, 'language', None),
                    language_probability=getattr(info, 'language_probability', None),
                )

            task = asyncio.create_task(asyncio.to_thread(run))
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout_seconds)
            except TimeoutError as exc:
                release_on_exit = False
                task.add_done_callback(lambda completed: self._release_task_slot(completed, cleanup_directory))
                raise AudioTimeoutError() from exc
            except asyncio.CancelledError:
                release_on_exit = False
                task.add_done_callback(lambda completed: self._release_task_slot(completed, cleanup_directory))
                raise

            log.info(
                'audio_stt_completed',
                extra={
                    'engine': 'whisper',
                    'queue_ms': round(queue_ms, 2),
                    'detected_language': result.language,
                },
            )
            self._record_transcription(queue_started, 'success')
            return result
        except AudioServiceError as exc:
            self._record_transcription(queue_started, exc.__class__.__name__)
            raise
        except asyncio.CancelledError:
            self._record_transcription(queue_started, 'cancelled')
            raise
        except Exception as exc:
            self._record_transcription(queue_started, 'error')
            log.exception('Local Whisper transcription failed')
            raise AudioModelError() from exc
        finally:
            if release_on_exit:
                self._release_slot()

    def _release_slot(self) -> None:
        self._active = max(0, self._active - 1)
        self._semaphore.release()

    @staticmethod
    def _record_transcription(started: float, outcome: str) -> None:
        attributes = {'engine': 'whisper', 'outcome': outcome}
        _stt_requests.add(1, attributes)
        _stt_duration.record((time.monotonic() - started) * 1000, attributes)

    def _release_task_slot(
        self,
        task: asyncio.Task[Any],
        cleanup_directory: str | None = None,
    ) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        if cleanup_directory:
            shutil.rmtree(cleanup_directory, ignore_errors=True)
        self._release_slot()

    @staticmethod
    def _observe_background_task(task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def readiness(self) -> dict[str, Any]:
        return {
            'loaded': self.loaded,
            'model': Path(self.model_name).name,
            'compute_type': self.compute_type,
            'active_transcriptions': self.active,
        }


def _signature_matches(extension: str, header: bytes) -> bool:
    if extension == 'wav':
        return len(header) >= 12 and header.startswith(b'RIFF') and header[8:12] == b'WAVE'
    if extension == 'flac':
        return header.startswith(b'fLaC')
    if extension in {'ogg', 'oga'}:
        return header.startswith(b'OggS')
    if extension in {'webm'}:
        return header.startswith(b'\x1aE\xdf\xa3')
    if extension in {'m4a', 'mp4'}:
        return len(header) >= 12 and header[4:8] == b'ftyp'
    if extension in {'mp3', 'mpga', 'mpeg'}:
        return header.startswith(b'ID3') or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0)
    return True


def _validate_upload_metadata(
    upload: UploadFile,
    allowed_extensions: set[str],
    supported_content_types: list[str],
) -> str:
    filename = os.path.basename(upload.filename or '')
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    content_type = (upload.content_type or '').split(';', 1)[0].strip().lower()

    if not extension or (allowed_extensions and extension not in allowed_extensions):
        raise AudioUnsupportedMediaError()
    if supported_content_types:
        supported = any(
            content_type == pattern.lower()
            or (pattern.endswith('/*') and content_type.startswith(pattern[:-1].lower()))
            for pattern in supported_content_types
        )
        if not supported:
            raise AudioUnsupportedMediaError()
    elif not (content_type.startswith('audio/') or content_type in {'video/mp4', 'video/webm'}):
        raise AudioUnsupportedMediaError()
    expected_content_types = EXTENSION_CONTENT_TYPES.get(extension)
    if expected_content_types and content_type not in expected_content_types:
        raise AudioUnsupportedMediaError()
    return extension


async def save_validated_upload(
    upload: UploadFile,
    destination: Path,
    *,
    max_bytes: int,
    allowed_extensions: set[str],
    supported_content_types: list[str],
) -> int:
    """Stream an upload to disk with a hard byte bound and basic signature checks."""
    extension = _validate_upload_metadata(upload, allowed_extensions, supported_content_types)

    total = 0
    header = b''
    try:
        with destination.open('xb') as output:
            while True:
                chunk = await upload.read(min(UPLOAD_CHUNK_SIZE, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AudioUploadTooLargeError()
                if len(header) < 16:
                    header = (header + chunk)[:16]
                output.write(chunk)
        if total == 0 or not _signature_matches(extension, header):
            raise AudioUnsupportedMediaError()
        return total
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


class AudioCacheManager:
    """Best-effort TTL and entry-count cleanup for generated speech cache files."""

    def __init__(self, directory: Path, *, ttl_seconds: int, max_entries: int) -> None:
        self.directory = directory
        self.ttl_seconds = max(0, ttl_seconds)
        self.max_entries = max(0, max_entries)
        self._cleanup_lock = asyncio.Lock()

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.debug('Could not remove an in-use audio cache entry')

    def _entry_groups(self) -> dict[str, tuple[float, list[Path]]]:
        groups: dict[str, tuple[float, list[Path]]] = {}
        for path in self.directory.iterdir():
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            previous = groups.get(path.stem)
            paths = [path] if previous is None else [*previous[1], path]
            groups[path.stem] = (mtime if previous is None else max(previous[0], mtime), paths)
        return groups

    def _cleanup(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        groups = self._entry_groups()

        for stem, (mtime, paths) in list(groups.items()):
            if self.ttl_seconds and now - mtime > self.ttl_seconds:
                for path in paths:
                    self._unlink(path)
                groups.pop(stem, None)

        if self.max_entries and len(groups) > self.max_entries:
            excess = len(groups) - self.max_entries
            for _, paths in sorted(groups.values(), key=lambda item: item[0])[:excess]:
                for path in paths:
                    self._unlink(path)

    async def cleanup(self) -> None:
        if self._cleanup_lock.locked():
            return
        async with self._cleanup_lock:
            await asyncio.to_thread(self._cleanup)

    def readiness(self) -> dict[str, Any]:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=True):
                pass
            writable = True
        except OSError:
            writable = False
        return {
            'writable': writable,
            'ttl_seconds': self.ttl_seconds,
            'max_entries': self.max_entries,
        }


def ffmpeg_readiness() -> dict[str, bool]:
    return configure_pydub_audio()
