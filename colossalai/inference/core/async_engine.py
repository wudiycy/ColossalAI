import asyncio
from functools import partial
from logging import Logger
from typing import AsyncIterator, Dict, Iterable, List, Optional, Set, Tuple, Type

from colossalai.inference.core.engine import InferenceEngine


class AsyncEngineDeadError(RuntimeError):
    pass


def _raise_exception_on_finish(task: asyncio.Task, request_tracker: "RequestTracker") -> None:
    msg = "Task finished unexpectedly. This should never happen! "
    try:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            raise AsyncEngineDeadError(msg + " See stack trace above for the actual cause.") from exc
        raise AsyncEngineDeadError(msg)
    except Exception as exc:
        request_tracker.propagate_exception(exc)
        raise exc


class AsyncStream:
    """A stream of Output for a request that can be
    iterated over asynchronously."""

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        self._future = asyncio.Future()

    def set_result(self, result) -> None:
        """Set final result and  signal taht it's ready"""
        if not self._future.done():
            self._future.set_result(result)

    async def get_result(self):
        """Wait for the result to be set and return it."""
        return await self._future

    @property
    def finished(self) -> bool:
        """Check if the stream has finished by checking if the future is done."""
        return self._future.done()


class RequestTracker:
    """Synchronous abstraction for tracking requests."""

    def __init__(self) -> None:
        self._request_streams: Dict[int, AsyncStream] = {}
        self._finished_requests: asyncio.Queue[int] = asyncio.Queue()
        self._new_requests: asyncio.Queue[Tuple[AsyncStream, dict]] = asyncio.Queue()
        self.new_requests_event = None

    def __contains__(self, item):
        return item in self._request_streams

    def init_event(self):
        self.new_requests_event = asyncio.Event()

    def propagate_exception(self, exc: Exception, request_id: Optional[int] = None) -> None:
        """
        Propagate an exception to request streams (all if request_id is None).
        """
        if request_id is not None:
            self._request_streams[request_id].set_result(exc)
        else:
            for stream in self._request_streams.values():
                stream.set_result(exc)

    def process_finished_request(self, finished_request) -> None:
        """Process a finished request from the engine."""
        request_id = finished_request.request_id
        try:
            self._request_streams[request_id].set_result(finished_request)
        except:
            raise RuntimeError(f"The request_id {request_id} is not found in our stream, please check")
        self.abort_request(request_id)

    def add_request(self, request_id: int, **engine_add_request_kwargs) -> AsyncStream:
        """
        Add a request to be sent to the engine on the next background
        loop iteration.
        """
        if request_id in self._request_streams:
            raise KeyError(f"Request {request_id} already exists.")

        stream = AsyncStream(request_id)
        self._new_requests.put_nowait((stream, {"request_id": request_id, **engine_add_request_kwargs}))

        self.new_requests_event.set()

        return stream

    def abort_request(self, request_id: int, *, verbose: bool = False) -> None:
        """Abort a request during next background loop iteration."""
        if verbose:
            Logger.info(f"Aborted request {request_id}.")

        self._finished_requests.put_nowait(request_id)

        if request_id not in self._request_streams or self._request_streams[request_id].finished:
            # The request has already finished or been aborted.
            return

        self._request_streams[request_id].set_result(None)

    def get_new_requests(self):
        """
        Get new requests from http server.
        """
        new_requests: List[Dict] = []

        while not self._new_requests.empty():
            stream, new_request = self._new_requests.get_nowait()
            self._request_streams[stream.request_id] = stream
            new_requests.append(new_request)

        self.new_requests_event.clear()

        return new_requests

    def get_new_and_finished_requests(self) -> Tuple[List[Dict], Set[int]]:
        """Get the new requests and finished requests to be
        sent to the engine."""
        new_requests: List[Dict] = []
        finished_requests: Set[int] = set()

        while not self._finished_requests.empty():
            request_id = self._finished_requests.get_nowait()
            finished_requests.add(request_id)
            self._request_streams.pop(request_id, None)

        while not self._new_requests.empty():
            stream, new_request = self._new_requests.get_nowait()
            if stream.request_id in finished_requests:
                # The request has already been aborted.
                stream.finish()
                continue
            self._request_streams[stream.request_id] = stream
            new_requests.append(new_request)

        self.new_requests_event.clear()

        return new_requests, finished_requests

    async def wait_for_new_requests(self):
        await self.new_requests_event.wait()


class _AsyncInferenceEngine(InferenceEngine):
    """
    Async methods for Inference Engine.
    """

    async def async_step(self) -> List[str]:
        """
        The async version of Engine.step()
        Performs one decoding iteration and returns newly generated results.

        It first schedules the sequences to be executed in the next iteration.
        Then, it executes the model and updates the scheduler with the model
        outputs. Finally, it decodes the sequences and returns the newly
        generated results.
        """
        batch = self.request_handler.schedule()
        loop = asyncio.get_running_loop()

        # Use run_in_executor to asyncally run the sync method model.forward().
        logits = await loop.run_in_executor(
            None,
            self.model,
            batch,
            self.k_cache,
            self.v_cache,
        )

        if self.inference_config.pad_input:
            logits = logits[:, -1, :]
        self.request_handler.search_tokens(self.generation_config, logits)
        # Return: List[Sequence]
        finished_sequences = self.request_handler.update()

        return finished_sequences, self.request_handler.current_requests_in_batch() > 0

    def _process_outputs(self, sequences):
        for sequence in sequences:
            sequence.output = self.tokenizer.decode(sequence.output_token_id)


class AsyncInferenceEngine:
    """An asynchronous wrapper for LLMEngine.

    This class is used to wrap the InferenceEngine class to make it asynchronous.
    It uses asyncio to create a background loop that keeps processing incoming
    requests. The LLMEngine is kicked by the generate method when there are
    requests in the waiting queue. The generate method yields the outputs
    from the InferenceEngine to the caller.
    """

    _engine_class: Type[_AsyncInferenceEngine] = _AsyncInferenceEngine

    def __init__(self, start_engine_loop: bool = True, **kwargs):
        self.engine = self._init_engine(**kwargs)
        self.background_loop = None
        # reference to the unshielded loop
        self._background_loop_unshielded = None
        self.start_engine_loop = start_engine_loop
        self._request_tracker = RequestTracker()

    @property
    def background_loop_status(self):
        return self.background_loop is not None and not self.background_loop.done()

    def start_background_loop(self):
        if self.background_loop_status:
            raise RuntimeError("Existing loop is running")

        self._request_tracker.init_event()

        self._background_loop_unshielded = asyncio.get_event_loop().create_task(self.run_engine_loop())
        self._background_loop_unshielded.add_done_callback(
            partial(_raise_exception_on_finish, request_tracker=self._request_tracker)
        )
        self.background_loop = asyncio.shield(self._background_loop_unshielded)

    def _init_engine(self, **kwargs):
        return self._engine_class(**kwargs)

    async def step(self):
        """
        Run engine to process requests

        Returns True if there are in-progress requests.
        """
        new_requests = self._request_tracker.get_new_requests()
        for new_request in new_requests:
            self.engine.add_single_request(**new_request)
        newly_finished_seqs, has_running_requests = await self.engine.async_step()
        self.engine._process_outputs(newly_finished_seqs)
        for seq in newly_finished_seqs:
            self._request_tracker.process_finished_request(seq)

        return has_running_requests

    async def _engine_abort(self, request_ids: Iterable[int]):
        self.engine.abort_request(request_ids)

    async def abort(self, request_id: int):
        """
        Abort a single request
        """
        if not self.background_loop_status:
            raise RuntimeError("Background loop is not running or launched correctly.")
        return self._abort(request_id)

    def _abort(self, request_id: int):
        self._request_tracker.abort_request(request_id)

    async def run_engine_loop(self):
        processing_requests = False
        while True:
            if not processing_requests:
                await self._request_tracker.wait_for_new_requests()
            processing_requests = await self.step()
            await asyncio.sleep(0)

    async def add_request(
        self,
        request_id: int,
        prompt: Optional[str],
        prompt_token_ids: Optional[List[int]] = None,
    ) -> AsyncStream:
        """
        Add a request to the background tracker(waitting queue), start the background loop if needed.
        """
        if not self.background_loop_status:
            if self.start_engine_loop:
                self.start_background_loop()
            else:
                raise RuntimeError("Background loop is not running.")
        stream = self._request_tracker.add_request(
            request_id,
            prompt=prompt,
            prompt_token_ids=prompt_token_ids,
        )
        return stream

    async def generate(
        self,
        request_id: int,
        prompt: Optional[str],
        prompt_token_ids: Optional[List[int]] = None,
    ) -> AsyncIterator[str]:
        """
        Generate output from a request. It receives the request from http server, adds it into the
        waitting queue of Async Engine and streams the output sequence.

        """
        try:
            stream = await self.add_request(request_id, prompt, prompt_token_ids=prompt_token_ids)
            return await stream.get_result()

        except (Exception, asyncio.CancelledError) as e:
            # If there is an exception or coroutine is cancelled, abort the
            # request.
            self._abort(request_id)
            raise e
