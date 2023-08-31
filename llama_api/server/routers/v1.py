"""V1 Endpoints for Local Llama API
Use same format as OpenAI API"""

from asyncio import (
    FIRST_COMPLETED,
    create_task,
    ensure_future,
    sleep,
    wait,
)
from dataclasses import dataclass, field
from functools import partial
from queue import Queue
from random import choice
from typing import Any, Dict, Iterator, Optional, Tuple, Type, TypeVar, Union

from anyio import (
    Semaphore,
    create_memory_object_stream,
    get_cancelled_exc_class,
)
from fastapi import APIRouter, Request
from fastapi.concurrency import iterate_in_threadpool, run_in_threadpool
from orjson import dumps
from sse_starlette.sse import EventSourceResponse

from ...schemas.api import (
    ChatCompletion,
    Completion,
    CreateChatCompletionRequest,
    CreateCompletionRequest,
    CreateEmbeddingRequest,
    Embedding,
    ModelData,
    ModelList,
)
from ...shared.config import MainCliArgs
from ...utils.concurrency import (
    get_queue_and_event,
    run_in_processpool_with_wix,
)
from ...utils.errors import RouteErrorHandler
from ...utils.logger import ApiLogger
from ..pools.llama import (
    generate_completion,
    generate_completion_chunks,
    generate_embeddings,
    get_model_names,
)

logger = ApiLogger(__name__)
router = APIRouter(prefix="/v1", route_class=RouteErrorHandler)
max_workers = int(MainCliArgs.max_workers.value or 1)
max_semaphores = int(MainCliArgs.max_semaphores.value or 1)
loop_timeout = 30.0
T = TypeVar("T")


@dataclass
class WixMetadata:
    """Worker index (wix) metadata"""

    wix: int
    processed_key: Optional[str] = None
    semaphore: Semaphore = field(
        default_factory=lambda: Semaphore(max_semaphores)
    )


# Worker index (wix) is used to keep track of which worker is currently
# processing a request. This is used to prevent multiple requests from
# creating multiple completion generators at the same time.
wix_metas: Tuple[WixMetadata] = tuple(
    WixMetadata(wix) for wix in range(max_workers)
)


def get_worker_rank(meta: WixMetadata, request_key: Optional[str]) -> int:
    """Get the entry rank for the worker index (wix) metadata.
    Lower rank means higher priority of the worker to process the request."""
    global max_semaphores
    if request_key == meta.processed_key:
        # If the key is the same (worker is processing the same model)
        return -2  # return the highest priority
    if request_key is None or meta.processed_key is None:
        # If not requesting a specific model or the worker is not processing
        return -1  # return the second highest priority
    return (
        max_semaphores - meta.semaphore.value
    )  # return the number of slots in use


async def get_wix_with_semaphore(
    request: Request,
    request_key: Optional[str] = None,
) -> int:
    """Get the worker index (wix) for the key and acquire the semaphore"""
    global wix_metas

    # Get the worker index (wix) with the lowest rank
    # If the rank is -2, then the worker is processing the same model
    # If the rank is -1, then the worker is not processing any model
    # If the rank is greater than or equal to 0, then the worker is processing
    # a different model
    worker_ranks = [
        get_worker_rank(wix_meta, request_key) for wix_meta in wix_metas
    ]
    min_rank = min(worker_ranks)

    # Choose a random worker index (wix) with the lowest rank
    candidates = [i for i, rank in enumerate(worker_ranks) if rank == min_rank]
    if not candidates:
        raise LookupError("No available wix")
    wix_meta = wix_metas[choice(candidates)]

    # Acquire the semaphore for the worker index (wix)
    await wix_meta.semaphore.acquire()
    # If client is already gone, then ignore the request
    if await request.is_disconnected():
        raise get_cancelled_exc_class()()
    # Reserve the worker, it is now processing the request
    wix_meta.processed_key = request_key
    return wix_meta.wix


def validate_item_type(item: Any, type: Type[T]) -> T:
    """Validate that the item is of the correct type"""
    if isinstance(item, Exception):
        # The producer task has raised an exception
        raise item
    elif not isinstance(item, type):
        # The producer task has returned an invalid response
        raise TypeError(f"Expected type {type}, but got {type(item)} instead")
    return item


def get_streaming_iterator(
    queue: Queue,
    first_response: Optional[Dict] = None,
    timeout: Optional[float] = None,
) -> Iterator[Dict]:
    """Get an iterator for the streaming of completion generator"""
    if first_response is not None:
        yield first_response

    while True:
        gen = queue.get(timeout=timeout)
        if gen is None:
            # The producer task is done
            break
        yield validate_item_type(gen, type=dict)


async def check_disconnection(request: Request):
    while True:
        await sleep(1.0)
        if await request.is_disconnected():
            raise get_cancelled_exc_class()()


async def create_chat_completion_or_completion(
    request: Request,
    body: Union[CreateChatCompletionRequest, CreateCompletionRequest],
    loop_timeout: float = loop_timeout,
) -> Union[EventSourceResponse, ChatCompletion, Completion]:
    """Create a chat completion or completion based on the body.
    If the body is a chat completion, then create a chat completion.
    If the body is a completion, then create a completion.
    If streaming is enabled, then return an EventSourceResponse."""
    wix = interrupt_signal = task = None
    try:
        wix = await get_wix_with_semaphore(request, body.model)
        queue, interrupt_signal = get_queue_and_event()
        task = create_task(
            run_in_processpool_with_wix(
                partial(
                    generate_completion_chunks
                    if body.stream
                    else generate_completion,
                    body=body,
                    queue=queue,
                    interrupt_signal=interrupt_signal,
                ),
                wix=wix,
            )
        )
        done, pending = await wait(
            {
                ensure_future(run_in_threadpool(queue.get)),
                ensure_future(check_disconnection(request)),
            },
            return_when=FIRST_COMPLETED,
        )
        if pending:
            for task in pending:
                task.cancel()
        if not done:
            logger.warning("🦙 Client disconnected")
            raise get_cancelled_exc_class()()

        first_response = validate_item_type(
            done.pop().result(), type=dict  # type: ignore
        )

        if body.stream:
            # Publish Server-Sent-Events (SSE) to the client
            send_chan, recv_chan = create_memory_object_stream(10)

            async def get_event_publisher() -> None:
                try:
                    send = send_chan.send
                    async for chunk in iterate_in_threadpool(
                        get_streaming_iterator(
                            queue=queue,
                            first_response=first_response,
                            timeout=loop_timeout,
                        )
                    ):
                        await send(b"data: " + dumps(chunk) + b"\n\n")
                    await send(b"data: [DONE]\n\n")
                finally:
                    # Cancel the producer task and set event,
                    # so the completion task can be stopped
                    interrupt_signal.set()
                    task.cancel()
                    send_chan.close()

            return EventSourceResponse(
                recv_chan,
                data_sender_callable=get_event_publisher,
            )
        else:
            # Return the first response, which is the completion
            return first_response  # type: ignore
    finally:
        if not body.stream and interrupt_signal is not None:
            interrupt_signal.set()
        if not body.stream and task is not None:
            task.cancel()
        if wix is not None:
            # Release the semaphore for the worker index (wix)
            wix_metas[wix].semaphore.release()


@router.post("/chat/completions")
async def create_chat_completion(
    request: Request, body: CreateChatCompletionRequest
):
    return await create_chat_completion_or_completion(
        request=request, body=body
    )


@router.post("/v1/engines/copilot-codex/completions")
@router.post("/completions")
async def create_completion(request: Request, body: CreateCompletionRequest):
    return await create_chat_completion_or_completion(
        request=request, body=body
    )


@router.post("/embeddings")
async def create_embedding(
    request: Request, body: CreateEmbeddingRequest
) -> Embedding:
    if MainCliArgs.no_embed.value:
        raise PermissionError("Embeddings endpoint is disabled")
    assert body.model is not None, "Model is required"
    wix = interrupt_signal = task = None
    try:
        wix = await get_wix_with_semaphore(request, body.model)
        queue, interrupt_signal = get_queue_and_event()
        task = create_task(
            run_in_processpool_with_wix(
                partial(
                    generate_embeddings,
                    body=body,
                    queue=queue,
                ),
                wix=wix,
            )
        )
        return validate_item_type(
            await run_in_threadpool(queue.get),
            type=dict,  # type: ignore
        )
    finally:
        if interrupt_signal is not None:
            interrupt_signal.set()
        if task is not None:
            task.cancel()
        if wix is not None:
            # Release the semaphore for the worker index (wix)
            wix_metas[wix].semaphore.release()


@router.get("/models")
async def get_models(request: Request) -> ModelList:
    wix: int = await get_wix_with_semaphore(request)
    try:
        return ModelList(
            object="list",
            data=[
                ModelData(
                    id=model_name,
                    object="model",
                    owned_by="me",
                    permissions=[],
                )
                for model_name in await run_in_processpool_with_wix(
                    get_model_names,
                    wix=wix,
                )
            ],
        )
    finally:
        # Release the semaphore for the worker index (wix)
        wix_metas[wix].semaphore.release()
