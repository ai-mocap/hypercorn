from functools import partial
from typing import Any, Awaitable, Callable, Generator, Optional

import trio

from ..config import Config
from ..events import Closed, Event, RawData
from ..protocol import ProtocolWrapper
from ..typing import ASGIFramework
from ..utils import invoke_asgi, parse_socket_addr

MAX_RECV = 2 ** 16


class EventWrapper:
    def __init__(self) -> None:
        self._event = trio.Event()

    async def clear(self) -> None:
        self._event.clear()

    async def wait(self) -> None:
        await self._event.wait()

    async def set(self) -> None:
        self._event.set()


async def _handle(
    app: ASGIFramework, config: Config, scope: dict, receive: Callable, send: Callable
) -> None:
    try:
        await invoke_asgi(app, scope, receive, send)
    except trio.Cancelled:
        raise
    except trio.MultiError as error:
        errors = error.filter(lambda exc: None if isinstance(exc, trio.Cancelled) else exc)
        if errors is not None:
            await config.log.exception("Error in ASGI Framework")
            await send(None)
        else:
            raise
    except Exception:
        await config.log.exception("Error in ASGI Framework")
        await send(None)


async def spawn_app(
    nursery: trio._core._run.Nursery,
    app: ASGIFramework,
    config: Config,
    scope: dict,
    send: Callable[[dict], Awaitable[None]],
) -> Callable[[dict], Awaitable[None]]:
    app_send_channel, app_receive_channel = trio.open_memory_channel(config.max_app_queue_size)
    nursery.start_soon(_handle, app, config, scope, app_receive_channel.receive, send)
    return app_send_channel.send


class Server:
    def __init__(self, app: ASGIFramework, config: Config, stream: trio.abc.Stream) -> None:
        self.app = app
        self.config = config
        self.protocol: ProtocolWrapper
        self.send_lock = trio.Lock()
        self.stream = stream

        self._keep_alive_timeout_handle: Optional[trio.CancelScope] = None

    def __await__(self) -> Generator[Any, None, None]:
        return self.run().__await__()

    async def run(self) -> None:
        try:
            try:
                with trio.fail_after(self.config.ssl_handshake_timeout):
                    await self.stream.do_handshake()
            except (trio.BrokenResourceError, trio.TooSlowError):
                return  # Handshake failed
            alpn_protocol = self.stream.selected_alpn_protocol()
            socket = self.stream.transport_stream.socket
            ssl = True
        except AttributeError:  # Not SSL
            alpn_protocol = "http/1.1"
            socket = self.stream.socket
            ssl = False

        client = parse_socket_addr(socket.family, socket.getpeername())
        server = parse_socket_addr(socket.family, socket.getsockname())

        try:
            async with trio.open_nursery() as nursery:
                self.nursery = nursery
                self.protocol = ProtocolWrapper(
                    self.config,
                    ssl,
                    client,
                    server,
                    self.protocol_send,
                    partial(spawn_app, nursery, self.app, self.config),
                    EventWrapper,
                    alpn_protocol,
                )
                await self.protocol.initiate()
                await self._update_keep_alive_timeout()
                await self._read_data()
        except trio.MultiError:
            pass
        finally:
            await self._close()

    async def protocol_send(self, event: Event) -> None:
        if isinstance(event, RawData):
            async with self.send_lock:
                try:
                    await self.stream.send_all(event.data)
                except trio.BrokenResourceError:
                    pass  # Allow ASGI Apps to finish
        elif isinstance(event, Closed):
            await self._close()
        await self._update_keep_alive_timeout()

    async def _read_data(self) -> None:
        while True:
            try:
                data = await self.stream.receive_some(MAX_RECV)
            except trio.TooSlowError:
                await self.protocol.handle(Closed())
                await self._close()
            except (trio.ClosedResourceError, trio.BrokenResourceError):
                break
            else:
                await self.protocol.handle(RawData(data))
                await self._update_keep_alive_timeout()
                if data == b"":
                    break

    async def _close(self) -> None:
        try:
            await self.stream.send_eof()
        except (
            trio.BrokenResourceError,
            AttributeError,
            trio.BusyResourceError,
            trio.ClosedResourceError,
        ):
            # They're already gone, nothing to do
            # Or it is a SSL stream
            pass
        await self.stream.aclose()

    async def _update_keep_alive_timeout(self) -> None:
        if self._keep_alive_timeout_handle is not None:
            self._keep_alive_timeout_handle.cancel()
        self._keep_alive_timeout_handle = None
        if self.protocol.idle:
            self._keep_alive_timeout_handle = await self.nursery.start(
                _call_later, self.config.keep_alive_timeout, self.stream.aclose
            )


async def _call_later(
    timeout: float,
    callback: Callable,
    task_status: trio._core._run._TaskStatus = trio.TASK_STATUS_IGNORED,
) -> None:
    cancel_scope = trio.CancelScope()
    task_status.started(cancel_scope)
    with cancel_scope:
        await trio.sleep(timeout)
        cancel_scope.shield = True
        await callback()
