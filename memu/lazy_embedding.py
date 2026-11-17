"""One shared, on-demand worker; idle expiry releases the entire Torch runtime."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading

from .gemma_embedding import describe_model, memory_snapshot
from .models import ModelOutputError, prepare_embedding_items, validate_embeddings


class LazyGemmaEmbedding:
    dimension = 768

    def __init__(self, config):
        self.config = config
        self._capabilities = None
        self._lock = threading.RLock()
        self._child = None
        self._finished = None
        self._timer = None
        self._generation = 0
        self._closed = False
        self.worker_starts = 0
        self.minimum_available_bytes = memory_snapshot()['available_bytes']

    @property
    def capabilities(self):
        with self._lock:
            if self._capabilities is None:
                self._capabilities = describe_model(self.config.model_path)
            return self._capabilities

    def _start(self):
        if memory_snapshot()['available_bytes'] < (2048 + 512 + 3072) * 1024 ** 2:
            raise MemoryError('Insufficient RAM to start embedding while retaining 2 GiB')
        environment = {name: os.environ[name] for name in ['PATH', 'LANG', 'LC_ALL', 'TMPDIR'] if name in os.environ}
        environment.update({
            'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'PYTHONUNBUFFERED': '1',
            'CUDA_VISIBLE_DEVICES': '', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
            'HF_HUB_DISABLE_TELEMETRY': '1', 'HF_HUB_DISABLE_PROGRESS_BARS': '1',
            'HF_HOME': '/tmp/memu-huggingface',
            'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': str(self.config.threads),
            'MKL_NUM_THREADS': str(self.config.threads), 'OPENBLAS_NUM_THREADS': '1',
        })
        child = subprocess.Popen([sys.executable, '-m', 'memu.embedding_worker',
                                  '--model-path', self.config.model_path, '--threads', str(self.config.threads)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment)
        finished = threading.Event()
        self._child, self._finished = child, finished
        self.worker_starts += 1

        def waiter():
            child.wait()
            finished.set()

        def memory_guard():
            # A resource interlock, not routine job-status polling. Runs only while a worker exists.
            while not finished.wait(0.25):
                available = memory_snapshot()['available_bytes']
                self.minimum_available_bytes = min(self.minimum_available_bytes, available)
                if available < (2048 + 256) * 1024 ** 2:
                    child.kill()
                    return

        threading.Thread(target=waiter, daemon=True, name='memu-embedding-exit').start()
        threading.Thread(target=memory_guard, daemon=True, name='memu-embedding-ram').start()

    def _stop(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        child, finished = self._child, self._finished
        self._child = self._finished = None
        if child is not None:
            try:
                child.stdin.close()
            except OSError:
                pass
            if not finished.wait(10):
                child.kill()
                finished.wait()
            child.stdout.close()

    def _expire(self, generation):
        with self._lock:
            if generation == self._generation:
                self._stop()

    def embed(self, items):
        with self._lock:
            if self._closed:
                raise RuntimeError('Embedding adapter is closed')
            prepared = prepare_embedding_items(items, self.capabilities)
            if not prepared:
                return []
            self._generation += 1
            if self._timer:
                self._timer.cancel()
            if self._finished is not None and self._finished.is_set():
                self._stop()
            if self._child is None:
                self._start()
            try:
                self._child.stdin.write((json.dumps({'items': prepared}) + '\n').encode())
                self._child.stdin.flush()
                ready, _, _ = select.select([self._child.stdout], [], [], self.config.timeout_seconds)
                if not ready:
                    raise TimeoutError('Embedding worker timed out')
                line = self._child.stdout.readline()
                if not line:
                    raise ModelOutputError('Embedding worker exited; check available memory')
                response = json.loads(line)
                if 'error' in response:
                    raise ModelOutputError('Embedding worker failed: ' + response['error'])
                result = validate_embeddings(prepared, response['embeddings'], self.capabilities)
            except Exception:
                self._stop()
                raise
            self._timer = threading.Timer(self.config.idle_seconds, self._expire, args=(self._generation,))
            self._timer.daemon = True
            self._timer.start()
            return result

    def close(self):
        with self._lock:
            self._closed = True
            self._stop()
