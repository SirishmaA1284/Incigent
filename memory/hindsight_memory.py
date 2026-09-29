"""
memory/hindsight_memory.py
Hindsight adapter. This is the only module that knows the Hindsight client API
(verified against hindsight-client 0.10.1):

    Hindsight(base_url, api_key=None, timeout=...)
    await client.aretain(bank_id, content, timestamp, context, document_id, metadata, tags)
    await client.arecall(bank_id, query, types, max_tokens, budget, tags, tags_match)
        -> RecallResponse.results: [RecallResult(id, text, type, context, metadata, tags,
                                                 occurred_start, document_id, scores, ...)]
    await client.areflect(bank_id, query, tags, tags_match) -> ReflectResponse.text
    await client.aclose()

The client's sync wrappers call run_until_complete() on the current loop and
fail inside a running loop (e.g. a FastAPI handler), and its aiohttp session is
bound to the loop that created it. So every call here builds a fresh client and
runs it with asyncio.run() on a short-lived worker thread.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional

from memory.base import MemoryRecord, MemoryUnavailableError, RecalledMemory


def _default_client_factory(base_url: str, api_key: Optional[str], timeout: float):
    try:
        from hindsight_client import Hindsight
    except ImportError as e:
        raise MemoryUnavailableError("hindsight-client is not installed") from e
    return Hindsight(base_url=base_url, api_key=api_key, timeout=timeout)


def retain_kwargs(bank_id: str, record: MemoryRecord) -> dict:
    """Arguments for Hindsight.aretain. metadata values must be strings."""
    return {
        "bank_id": bank_id,
        "content": record.content,
        "timestamp": record.occurred_at,
        "context": record.context,
        # Hindsight upserts by document_id: re-retaining the same ID replaces the
        # document and its memories instead of adding duplicates.
        "document_id": record.document_id,
        "metadata": {str(k): str(v) for k, v in record.metadata.items()},
        "tags": list(record.tags),
    }


def recall_kwargs(bank_id: str, query: str, tags: list, types, budget: str, max_tokens: int) -> dict:
    """Arguments for Hindsight.arecall. any_strict excludes untagged memories."""
    return {
        "bank_id": bank_id,
        "query": query,
        "types": types,
        "budget": budget,
        "max_tokens": max_tokens,
        "tags": list(tags) or None,
        "tags_match": "any_strict",
    }


def normalize_result(result) -> RecalledMemory:
    """Convert a Hindsight RecallResult into an application RecalledMemory."""
    metadata = getattr(result, "metadata", None) or {}
    scores = getattr(result, "scores", None)
    final = getattr(scores, "final", None) if scores is not None else None
    return RecalledMemory(
        text=result.text,
        source="hindsight",
        memory_type=getattr(result, "type", None),
        incident_id=metadata.get("incident_id"),
        event_type=metadata.get("event_type"),
        services=metadata.get("services"),
        occurred_at=metadata.get("occurred_at") or getattr(result, "occurred_start", None),
        tags=tuple(getattr(result, "tags", None) or ()),
        score=float(final) if final is not None else None,
    )


class HindsightMemory:
    backend = "hindsight"

    def __init__(self, base_url: str, bank_id: str, api_key: Optional[str] = None,
                 timeout: float = 30.0, recall_types: Optional[list] = None,
                 budget: str = "mid", max_tokens: int = 4096,
                 client_factory: Optional[Callable] = None):
        if not base_url or not bank_id:
            raise ValueError("base_url and bank_id are required")
        self.base_url = base_url
        self.bank_id = bank_id
        self._api_key = api_key
        self.timeout = timeout
        # None = all memory types. Hindsight decides whether an extracted fact is
        # "world", "experience" or "observation"; scoping is done with tags.
        self.recall_types = recall_types
        self.budget = budget
        self.max_tokens = max_tokens
        self._client_factory = client_factory or _default_client_factory

    @classmethod
    def from_env(cls):
        """Return (HindsightMemory | None, reason). None when not configured."""
        url = os.getenv("HINDSIGHT_API_URL", "").strip()
        bank = os.getenv("HINDSIGHT_BANK_ID", "").strip()
        if not url:
            return None, "HINDSIGHT_API_URL is not set"
        if not bank:
            return None, "HINDSIGHT_BANK_ID is not set"
        return cls(base_url=url, bank_id=bank, api_key=os.getenv("HINDSIGHT_API_KEY") or None), None

    # -- plumbing -------------------------------------------------------------
    def _run(self, operation):
        async def call():
            client = self._client_factory(self.base_url, self._api_key, self.timeout)
            try:
                return await operation(client)
            finally:
                await client.aclose()

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(asyncio.run, call())
            return future.result(timeout=self.timeout + 5)
        except MemoryUnavailableError:
            raise
        except Exception as e:
            if getattr(e, "status", None) == 404:
                raise _BankNotFound() from e
            raise MemoryUnavailableError(f"{type(e).__name__}: {e}") from e
        finally:
            # Don't block on a hung call; the worker ends when the client times out.
            pool.shutdown(wait=False)

    # -- OperationalMemory ----------------------------------------------------
    def retain_event(self, record: MemoryRecord) -> None:
        kwargs = retain_kwargs(self.bank_id, record)
        if isinstance(kwargs["timestamp"], datetime) and kwargs["timestamp"].tzinfo is None:
            kwargs["timestamp"] = kwargs["timestamp"].replace(tzinfo=timezone.utc)
        try:
            response = self._run(lambda client: client.aretain(**kwargs))
        except _BankNotFound as e:
            raise MemoryUnavailableError("Hindsight returned 404 for retain") from e
        if not getattr(response, "success", False):
            raise MemoryUnavailableError("Hindsight retain did not report success")

    def recall(self, query: str, *, tags: list, limit: int) -> list:
        kwargs = recall_kwargs(self.bank_id, query, tags, self.recall_types, self.budget, self.max_tokens)
        try:
            response = self._run(lambda client: client.arecall(**kwargs))
        except _BankNotFound:
            # Banks are created by the first write; reading a bank that does not
            # exist yet returns 404, which simply means no memories so far.
            return []
        return [normalize_result(r) for r in (response.results or [])][:limit]

    def reflect(self, query: str, *, tags: list) -> str:
        response = self._run(lambda client: client.areflect(
            bank_id=self.bank_id, query=query, tags=list(tags) or None, tags_match="any_strict"))
        return response.text


class _BankNotFound(Exception):
    pass
