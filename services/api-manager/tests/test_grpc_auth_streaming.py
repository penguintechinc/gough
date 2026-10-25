"""Streaming and fall-through paths of ``GrpcAuthInterceptor.intercept_service``.

The unary-unary path is covered in ``tests/test_grpc_auth.py``; this module
asserts that every RPC shape (unary-stream, stream-unary, stream-stream) is
gated identically: unauthenticated calls are aborted UNAUTHENTICATED and the
servicer body never runs, authenticated calls reach the servicer and yield its
results. Also covers the ``None`` handler and unknown-handler-shape pass-through.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest
from app.grpc_auth import GrpcAuthInterceptor

from tests.test_grpc_auth import FakeServicerContext, _AbortCalled, _HandlerCallDetails

MACHINE_CLAIMS = {
    "sub": "machine:discovery-agent",
    "tenant": "__default__",
    "scope": ["nodes:read"],
}


def _verifier(claims: dict[str, Any] | None = None) -> AsyncMock:
    """Return an OIDC verifier stub that accepts any token with ``claims``."""
    verifier = AsyncMock()
    verifier.verify_token.return_value = (
        claims if claims is not None else MACHINE_CLAIMS
    )
    return verifier


async def _resolve(
    interceptor: GrpcAuthInterceptor, handler: grpc.RpcMethodHandler
) -> Any:
    """Run intercept_service against a continuation returning ``handler``."""

    async def continuation(_details: Any) -> grpc.RpcMethodHandler:
        return handler

    return await interceptor.intercept_service(
        continuation, _HandlerCallDetails("/gough.Test/Method")
    )


async def _unary_stream_body(request: Any, context: Any) -> AsyncIterator[str]:
    """Yield two chunks derived from the request."""
    yield f"{request}-1"
    yield f"{request}-2"


async def _stream_unary_body(request_iterator: Any, context: Any) -> str:
    """Concatenate all streamed requests."""
    parts = [item async for item in request_iterator]
    return "+".join(parts)


async def _stream_stream_body(
    request_iterator: Any, context: Any
) -> AsyncIterator[str]:
    """Echo each streamed request upper-cased."""
    async for item in request_iterator:
        yield item.upper()


async def _aiter(items: list[str]) -> AsyncIterator[str]:
    """Turn a list into an async iterator, mimicking a grpc request stream."""
    for item in items:
        yield item


async def test_unary_stream_rejects_unauthenticated_and_never_runs_servicer() -> None:
    """Unary-stream RPC with no credentials is aborted before the first chunk."""
    handler = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=None),
        grpc.unary_stream_rpc_method_handler(_unary_stream_body),
    )
    assert handler is not None
    context = FakeServicerContext()

    with pytest.raises(_AbortCalled) as exc_info:
        async for _ in handler.unary_stream("req", context):
            pytest.fail("servicer must not yield for an unauthenticated RPC")

    assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED


async def test_unary_stream_authenticated_yields_all_chunks() -> None:
    """Unary-stream RPC with a valid machine JWT yields every servicer chunk."""
    handler = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=_verifier()),
        grpc.unary_stream_rpc_method_handler(_unary_stream_body),
    )
    assert handler is not None
    context = FakeServicerContext(metadata={"authorization": "Bearer t"})

    chunks = [chunk async for chunk in handler.unary_stream("req", context)]

    assert chunks == ["req-1", "req-2"]
    assert context.aborted is None


async def test_stream_unary_rejects_unauthenticated() -> None:
    """Stream-unary RPC with no credentials is aborted UNAUTHENTICATED."""
    handler = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=None),
        grpc.stream_unary_rpc_method_handler(_stream_unary_body),
    )
    assert handler is not None
    context = FakeServicerContext()

    with pytest.raises(_AbortCalled) as exc_info:
        await handler.stream_unary(_aiter(["a"]), context)

    assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED


async def test_stream_unary_authenticated_returns_servicer_result() -> None:
    """Stream-unary RPC with a valid JWT returns the servicer's aggregate result."""
    handler = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=_verifier()),
        grpc.stream_unary_rpc_method_handler(_stream_unary_body),
    )
    assert handler is not None
    context = FakeServicerContext(metadata={"authorization": "Bearer t"})

    result = await handler.stream_unary(_aiter(["a", "b", "c"]), context)

    assert result == "a+b+c"
    assert context.aborted is None


async def test_stream_stream_rejects_unauthenticated() -> None:
    """Bidirectional RPC with no credentials is aborted before any response."""
    handler = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=None),
        grpc.stream_stream_rpc_method_handler(_stream_stream_body),
    )
    assert handler is not None
    context = FakeServicerContext()

    with pytest.raises(_AbortCalled) as exc_info:
        async for _ in handler.stream_stream(_aiter(["x"]), context):
            pytest.fail("servicer must not respond to an unauthenticated RPC")

    assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED


async def test_stream_stream_authenticated_streams_responses() -> None:
    """Bidirectional RPC with a valid JWT streams the servicer's responses in order."""
    handler = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=_verifier()),
        grpc.stream_stream_rpc_method_handler(_stream_stream_body),
    )
    assert handler is not None
    context = FakeServicerContext(metadata={"authorization": "Bearer t"})

    out = [item async for item in handler.stream_stream(_aiter(["ab", "cd"]), context)]

    assert out == ["AB", "CD"]


async def test_stream_rejected_when_verifier_fails() -> None:
    """A stream RPC whose machine JWT fails verification is rejected, not served."""
    verifier = AsyncMock()
    verifier.verify_token.side_effect = ValueError("bad signature")
    handler = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=verifier),
        grpc.stream_unary_rpc_method_handler(_stream_unary_body),
    )
    assert handler is not None
    context = FakeServicerContext(metadata={"authorization": "Bearer forged"})

    with pytest.raises(_AbortCalled) as exc_info:
        await handler.stream_unary(_aiter(["a"]), context)

    assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED
    verifier.verify_token.assert_awaited_once_with("forged")


async def test_missing_handler_passes_through_as_none() -> None:
    """When the continuation resolves no handler, interception returns None unchanged."""
    interceptor = GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=None)

    assert await _resolve(interceptor, None) is None  # type: ignore[arg-type]


async def test_handler_without_rpc_shape_is_returned_unwrapped() -> None:
    """A handler with no unary/stream behaviour set is returned as-is (no auth wrapper)."""
    bare = grpc.unary_unary_rpc_method_handler(_stream_unary_body)._replace(
        unary_unary=None
    )
    resolved = await _resolve(
        GrpcAuthInterceptor(trust_bundle_pem=None, oidc_verifier=None),
        bare,
    )

    assert resolved is bare
