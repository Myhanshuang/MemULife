"""Core-only assembly of storage, database and model resources."""
import os
import threading


class DeferredEmbedding:
    """Provider discovery must not prevent durable Source admission at startup."""
    def __init__(self, factory):
        self.factory, self.adapter = factory, None
        self.lock = threading.RLock()

    def get(self):
        with self.lock:
            if self.adapter is None:
                adapter = self.factory()
                try:
                    if adapter.capabilities['dimension'] > 4000:
                        raise ValueError('Embedding dimension exceeds pgvector HNSW support')
                except Exception:
                    adapter.close()
                    raise
                self.adapter = adapter
            return self.adapter

    @property
    def capabilities(self):
        return self.get().capabilities

    def embed(self, items):
        return self.get().embed(items)

    def close(self):
        with self.lock:
            if self.adapter is not None:
                self.adapter.close()


def _build(root, config):
    from .index import PostgresIndex
    from .models import HTTPEmbedding, HTTPMLLM
    from .service import MemoryService
    from .postgres_repository import PostgresRepository

    resources = []
    try:
        model_type = HTTPMLLM
        if config.models.mllm.protocol == "llamacpp":
            from .llamacpp import LlamaCppMLLM
            model_type = LlamaCppMLLM
        model = model_type(config.models.mllm) if config.models.mllm.endpoint else None
        if model is not None:
            resources.append(model)
        if config.models.embedding.backend == "local_gemma":
            from .lazy_embedding import LazyGemmaEmbedding
            embedding = LazyGemmaEmbedding(config.models.embedding)
        elif config.models.embedding.backend == "vllm_gemma":
            from .vllm_embedding import VLLMGemmaEmbedding
            embedding = DeferredEmbedding(lambda: VLLMGemmaEmbedding(config.models.embedding))
        else:
            embedding = (DeferredEmbedding(lambda: HTTPEmbedding(config.models.embedding))
                         if config.models.embedding.endpoint else None)
        if embedding is not None:
            resources.append(embedding)
        dsn = os.environ.get(config.database.dsn_env)
        index = PostgresIndex(dsn, rrf_constant=config.read.rrf_constant) if dsn else None
        if index is not None:
            resources.append(index)
        if not dsn:
            raise ValueError('MEMU_DATABASE_URL is required for authoritative control storage')
        repository = PostgresRepository(root, dsn, config.repository.timezone)
        resources.append(repository)
        service = MemoryService(repository, config, model=model, embedder=embedding, index=index)
        return service, resources
    except Exception:
        for resource in reversed(resources):
            resource.close()
        raise
