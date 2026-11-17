"""Local, native EmbeddingGemma 2 adapter; all inference stays on the CPU.

API references:
https://ai.google.dev/gemma/docs/embeddinggemma/multimodal-embeddinggemma-with-sentence-transformers
https://sbert.net/docs/input_formats.html

This module does not import PyTorch or load weights until the adapter is created.
Its memory checks supplement an external cgroup limit; they cannot prevent a
different process from consuming memory between checks.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import resource
import tempfile
import threading
import time

from .models import prepare_embedding_items, validate_embeddings


MIN_FREE_BYTES = 2 * 1024**3


def memory_snapshot() -> dict:
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in {'MemTotal', 'MemAvailable'}:
            values[key] = int(value.strip().split()[0]) * 1024
    return {
        'available_bytes': values['MemAvailable'],
        'total_bytes': values['MemTotal'],
        'process_max_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    }


def require_memory_reserve(minimum: int = MIN_FREE_BYTES) -> dict:
    snapshot = memory_snapshot()
    if snapshot['available_bytes'] < minimum:
        raise RuntimeError('Stopped: less than the required 2 GiB system memory reserve remains')
    return snapshot


def _model_digest(directory: Path) -> str:
    """Hash actual local checkpoint bytes, without constructing tensor copies."""
    digest = hashlib.sha256()
    weights = sorted(directory.glob('*.safetensors'))
    if not weights:
        raise ValueError('Local safetensors weights are missing')
    for path in [directory / 'config.json', *weights]:
        digest.update(path.name.encode())
        digest.update(b'\0')
        with path.open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def decode_audio_file(path: Path, maximum_seconds: float = 30):
    """Decode actual audio bytes, preserving duration while normalizing to mono 16 kHz."""
    import numpy as np
    import soundfile as sf
    import soxr

    info = sf.info(path)
    if not 0 < info.duration <= maximum_seconds + 0.001 or not 1 <= info.channels <= 8:
        raise ValueError('Native audio exceeds the duration/channel limit')
    if not 1 <= info.samplerate <= 192000:
        raise ValueError('Unsupported native audio sample rate')
    samples, rate = sf.read(path, dtype='float32', always_2d=True)
    samples = samples.mean(axis=1)
    if rate != 16000:
        samples = soxr.resample(samples, rate, 16000)
    if not np.isfinite(samples).all():
        raise ValueError('Nonfinite native audio samples')
    return {'array': np.asarray(samples, dtype=np.float32), 'sampling_rate': 16000}, {
        'decoder': 'soundfile+soxr', 'duration_seconds': info.duration, 'sampling_rate': 16000,
    }


def decode_video_file(path: Path, maximum_seconds: float = 10):
    """Decode bounded CFR clips with PyAV, retaining their actual frame positions.

    This avoids the incompatible TorchCodec shared libraries on this host.
    VFR timestamps that cannot be represented faithfully by the processor's
    fps/frame-index metadata are rejected instead of assigning invented times.
    """
    import av
    import numpy as np

    with av.open(str(path), options={'protocol_whitelist': 'file,pipe'}) as container:
        if not container.streams.video:
            raise ValueError('Native video has no video stream')
        stream = container.streams.video[0]
        fps = float(stream.average_rate or stream.guessed_rate or 0)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('Native video has no usable frame rate')
        duration = (float(stream.duration * stream.time_base) if stream.duration is not None
                    else float(container.duration / av.time_base) if container.duration is not None else None)
        if duration is None or not 0 < duration <= maximum_seconds + 0.001:
            raise ValueError('Native video exceeds the duration limit or lacks a duration')
        stream.thread_count = 1
        frames, indices, timestamps = [], [], []
        first_time = None
        next_second = 0.0
        decoded_count = 0
        for number, frame in enumerate(container.decode(stream)):
            require_memory_reserve()
            if frame.pts is None or frame.time_base is None:
                raise ValueError('Native video frames require explicit timestamps')
            stamp = float(frame.pts * frame.time_base)
            first_time = stamp if first_time is None else first_time
            timestamp = stamp - first_time
            if timestamp > maximum_seconds or abs(timestamp - number / fps) > 0.001:
                raise ValueError('Video timestamps cannot be represented by the supported CFR metadata')
            decoded_count += 1
            if timestamp + 1e-6 < next_second:
                continue
            if frame.width > 8192 or frame.height > 8192:
                raise ValueError('Video resolution exceeds the bounded decoder limit')
            scale = min(1.0, 512 / max(frame.width, frame.height))
            width, height = max(1, round(frame.width * scale)), max(1, round(frame.height * scale))
            frames.append(frame.reformat(width=width, height=height, format='rgb24').to_ndarray())
            indices.append(number)
            timestamps.append(timestamp)
            next_second = math.floor(timestamp) + 1.0
        if not frames or len(frames) > math.ceil(maximum_seconds):
            raise ValueError('Video has no usable frames or exceeds the sample limit')
        array = np.stack(frames)
        metadata = {'total_num_frames': decoded_count, 'fps': fps, 'duration': duration,
                    'frames_indices': indices, 'width': array.shape[2], 'height': array.shape[1],
                    'video_backend': 'pyav'}
        return {'array': array, 'video_metadata': metadata}, {
            'decoder': 'PyAV', 'sampled_frames': len(frames), 'sampled_timestamps': timestamps,
            'source_fps': fps, 'duration_seconds': duration,
        }


def describe_model(model_path):
    """Stable capability metadata without importing Torch or loading tensors."""
    from importlib import metadata
    directory = Path(model_path).expanduser().resolve()
    config = json.loads((directory / 'config.json').read_text())
    if config.get('model_type') != 'embedding_gemma2':
        raise ValueError('The selected checkpoint is not EmbeddingGemma 2')
    versions = {name: metadata.version(name) for name in
                ['torch', 'transformers', 'sentence-transformers', 'safetensors',
                 'av', 'soundfile', 'soxr', 'jinja2']}
    digest = _model_digest(directory)
    preprocessing = {
        'dtype': 'bfloat16', 'device': 'cpu', 'dimension': 768,
        'text_prompts': {'query': 'SearchQuery', 'document': 'Document'},
        'media': 'native-audio-array16k-pyav-cfr1fps-maxside512', 'normalization': 'float32-l2',
        'libraries': versions,
        'processor_config_sha256': hashlib.sha256((directory / 'processor_config.json').read_bytes()).hexdigest(),
    }
    return {
        'modalities': ['text', 'image', 'audio', 'video'], 'verified_modalities': [],
        'dimension': 768, 'metric': 'cosine', 'space_id': 'embeddinggemma2-sha256:' + digest,
        'model_version': 'embeddinggemma2-sha256:' + digest,
        'preprocess_version': 'native-cpu-bf16:' + hashlib.sha256(
            json.dumps(preprocessing, sort_keys=True).encode()).hexdigest(),
        'max_duration_seconds': {'audio': 30, 'video': 10},
    }


class LocalGemmaEmbedding:
    """One resident model, batch size one, no downloads, captions, or GPU fallback."""

    def __init__(self, model_path: str | Path, *, threads: int = 2,
                 minimum_free_bytes: int = MIN_FREE_BYTES):
        if threads not in {1, 2, 4}:
            raise ValueError('Use one, two, or four CPU threads')
        if minimum_free_bytes < MIN_FREE_BYTES:
            raise ValueError('The 2 GiB memory reserve cannot be reduced')
        self.minimum_free_bytes = minimum_free_bytes
        require_memory_reserve(minimum_free_bytes)
        self.path = Path(model_path).expanduser().resolve()
        if not self.path.is_dir():
            raise ValueError('A local EmbeddingGemma 2 directory is required')
        config = json.loads((self.path / 'config.json').read_text())
        if config.get('model_type') != 'embedding_gemma2':
            raise ValueError('The selected checkpoint is not EmbeddingGemma 2')
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
        os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

        started = time.monotonic()
        from importlib import metadata
        import torch
        from sentence_transformers import SentenceTransformer

        self.torch = torch
        torch.set_num_threads(threads)
        self.library_versions = {name: metadata.version(name)
                                 for name in ['torch', 'transformers', 'sentence-transformers', 'safetensors',
                                              'av', 'soundfile', 'soxr', 'jinja2']}
        require_memory_reserve(minimum_free_bytes)
        checkpoint_digest = _model_digest(self.path)
        require_memory_reserve(minimum_free_bytes)
        self.model = SentenceTransformer(
            str(self.path), device='cpu', local_files_only=True, trust_remote_code=False,
            model_kwargs={'torch_dtype': torch.bfloat16, 'low_cpu_mem_usage': True},
        )
        self.model.eval()
        if self.model.device.type != 'cpu' or self.model.dtype != torch.bfloat16:
            raise RuntimeError('Local embedding must remain CPU BF16; no fallback is allowed')
        required = ['text', 'image', 'audio', 'video']
        supported = [kind for kind in required if self.model.supports(kind)]
        if supported != required:
            raise ValueError(f'Loaded model lacks native routes: {sorted(set(required) - set(supported))}')
        dimension_getter = getattr(self.model, 'get_embedding_dimension', None)
        if dimension_getter is None:
            dimension_getter = self.model.get_sentence_embedding_dimension
        if dimension_getter() != 768:
            raise ValueError('Expected the native 768-dimensional representation')
        preprocessing = {
            'dtype': 'bfloat16', 'device': 'cpu', 'dimension': 768,
            'text_prompts': {'query': 'SearchQuery', 'document': 'Document'},
            'media': 'native-audio-array16k-pyav-cfr1fps-maxside512', 'normalization': 'float32-l2',
            'libraries': self.library_versions,
            'processor_config_sha256': hashlib.sha256((self.path / 'processor_config.json').read_bytes()).hexdigest(),
        }
        self.capabilities = {
            'modalities': supported, 'verified_modalities': [], 'dimension': 768, 'metric': 'cosine',
            'space_id': 'embeddinggemma2-sha256:' + checkpoint_digest,
            'model_version': 'embeddinggemma2-sha256:' + checkpoint_digest,
            'preprocess_version': 'native-cpu-bf16:' + hashlib.sha256(
                json.dumps(preprocessing, sort_keys=True).encode()).hexdigest(),
            'max_duration_seconds': {'audio': 30, 'video': 10},
        }
        self.routes: list[dict] = []
        self._lock = threading.Lock()
        self.load_seconds = time.monotonic() - started
        self.load_memory = require_memory_reserve(minimum_free_bytes)

    def embed(self, items: list[dict]) -> list[dict]:
        results = []
        with self._lock:
            for original in items:
                if set(original) - {'kind', 'text', 'purpose', 'data_base64', 'mime_type', 'start', 'end'}:
                    raise ValueError('Local embedding accepts only text or supplied native media bytes')
                purpose = original.get('purpose', 'document')
                if purpose not in {'query', 'document'}:
                    raise ValueError('Embedding purpose must be query or document')
                item = prepare_embedding_items([original], self.capabilities)[0]
                before = require_memory_reserve(self.minimum_free_bytes)
                started = time.monotonic()
                with tempfile.TemporaryDirectory(prefix='memu-gemma-') as temporary:
                    kind = item['kind']
                    arguments = {'batch_size': 1, 'show_progress_bar': False,
                                 'convert_to_tensor': True, 'normalize_embeddings': True}
                    decoding = None
                    if kind == 'text':
                        payload = {'text': item['text']}
                        arguments['prompt_name'] = 'SearchQuery' if purpose == 'query' else 'Document'
                        byte_count = None
                    else:
                        encoded = base64.b64decode(item['data_base64'], validate=True)
                        if len(encoded) > 64 * 1024 * 1024:
                            raise ValueError('Native media input exceeds 64 MiB')
                        extension = mimetypes.guess_extension(item['mime_type']) or '.bin'
                        media_path = Path(temporary) / ('native' + extension)
                        media_path.write_bytes(encoded)
                        if kind == 'audio':
                            native, decoding = decode_audio_file(media_path)
                            payload = {'audio': native}
                        elif kind == 'video':
                            native, decoding = decode_video_file(media_path)
                            payload = {'video': native}
                            arguments['processing_kwargs'] = {'video': {'do_sample_frames': False}}
                        else:
                            payload = {kind: str(media_path)}
                        byte_count = len(encoded)
                        del encoded
                    with self.torch.inference_mode():
                        encoded_vector = self.model.encode([payload], **arguments)
                    vector = encoded_vector.detach().to(dtype=self.torch.float32, device='cpu').reshape(-1)
                    if vector.numel() != 768 or not self.torch.isfinite(vector).all():
                        raise RuntimeError('Model returned a nonfinite or wrong-dimensional embedding')
                    norm = vector.norm(p=2)
                    if norm.item() <= 0:
                        raise RuntimeError('Model returned a zero embedding')
                    vector = vector / norm
                    output = {
                        'vector': vector.tolist(), 'kind': kind,
                        **{key: self.capabilities[key] for key in
                           ['space_id', 'model_version', 'preprocess_version', 'metric']},
                    }
                    del encoded_vector, vector, norm
                after = require_memory_reserve(self.minimum_free_bytes)
                route = {'kind': kind, 'purpose': purpose, 'route': f'SentenceTransformer.encode/{kind}',
                         'native_media_bytes': byte_count, 'elapsed_seconds': time.monotonic() - started,
                         'memory_before': before, 'memory_after': after}
                if decoding:
                    route['decoding'] = decoding
                if kind in {'audio', 'video'}:
                    route.update(original_start=item['start'], original_end=item['end'])
                self.routes.append(route)
                del self.routes[:-64]
                if kind not in self.capabilities['verified_modalities']:
                    self.capabilities['verified_modalities'].append(kind)
                results.extend(validate_embeddings([item], [output], self.capabilities))
        return results

    def close(self):
        self.model = None
        gc.collect()
