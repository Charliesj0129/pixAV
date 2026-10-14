"""Activity context follows an await chain and cannot leak between workers."""

import asyncio
import uuid

import pytest

from pixav.shared.activity_context import activity_context, require_activity_context
from pixav.shared.workflow import ActivityRequest


def envelope():
    return ActivityRequest(
        task_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        owner=uuid.uuid4(),
        generation=7,
        stage="upload",
        identity="synthetic-operation",
        account_id=uuid.uuid4(),
    )


async def test_parallel_activity_contexts_keep_original_tokens_separate():
    first, second = envelope(), envelope()
    token_a, token_b = uuid.uuid4(), uuid.uuid4()
    ready, release = asyncio.Event(), asyncio.Event()

    async def owner(request, token):
        with activity_context(request, token):
            ready.set()
            await release.wait()
            context = require_activity_context()
            assert context.request is request and context.worker_token == token
        with pytest.raises(RuntimeError):
            require_activity_context()

    a = asyncio.create_task(owner(first, token_a))
    b = asyncio.create_task(owner(second, token_b))
    await ready.wait()
    with pytest.raises(RuntimeError):
        require_activity_context()
    release.set()
    await asyncio.gather(a, b)


def test_nested_activity_restores_parent_even_after_failure():
    parent, child = envelope(), envelope()
    with activity_context(parent, uuid.uuid4()) as original:
        with pytest.raises(ValueError):
            with activity_context(child, uuid.uuid4()):
                raise ValueError("synthetic child failure")
        assert require_activity_context() is original
    with pytest.raises(RuntimeError):
        require_activity_context()
