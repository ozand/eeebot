"""HTTPX adapter that pins each request to a validated DNS snapshot."""
from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator

import httpcore
import httpx


class PinnedAddressTransport(httpx.AsyncBaseTransport):
    """Preserve HTTP authority and TLS identity while pinning TCP destination."""

    def __init__(self, addresses: tuple[str, ...], hostname: str, port: int):
        if not addresses:
            raise ValueError("A non-empty validated address snapshot is required")
        self.addresses = tuple(addresses)
        self.hostname = hostname
        self.port = port
        self._pools: list[httpcore.AsyncConnectionPool] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if request.url.host.lower() != self.hostname.lower() or port != self.port:
            raise httpx.ConnectError("Request origin did not match validated URL", request=request)
        backend = _PinnedBackend(self.addresses, self.hostname, self.port)
        pool = httpcore.AsyncConnectionPool(
            network_backend=backend,
            retries=0,
            max_connections=1,
            max_keepalive_connections=0,
        )
        self._pools.append(pool)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.scheme,
                host=request.url.raw_host,
                port=port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = await pool.handle_async_request(core_request)
        except BaseException:
            await pool.aclose()
            raise
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PoolStream(pool, response.stream),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        for pool in self._pools:
            await pool.aclose()
        self._pools.clear()


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, addresses: tuple[str, ...], expected_host: str, expected_port: int):
        self.addresses = addresses
        self.expected_host = expected_host
        self.expected_port = expected_port

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host.lower() != self.expected_host.lower() or port != self.expected_port:
            raise httpcore.ConnectError("Connection origin did not match validated URL")
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        last_error = None
        for address in self.addresses:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                break
            try:
                operation = asyncio.open_connection(address, port, local_addr=local_address)
                if remaining is not None:
                    reader, writer = await asyncio.wait_for(operation, remaining)
                else:
                    reader, writer = await operation
                return _AsyncioNetworkStream(reader, writer)
            except (OSError, asyncio.TimeoutError, httpcore.ConnectError) as exc:
                last_error = exc
        if deadline is not None and loop.time() >= deadline:
            raise httpcore.ConnectTimeout("Connection attempts exhausted timeout") from last_error
        raise httpcore.ConnectError("All validated addresses failed") from last_error

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not permitted for web fetch")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class _AsyncioNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        operation = self.reader.read(max_bytes)
        return await asyncio.wait_for(operation, timeout) if timeout is not None else await operation

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writer.write(buffer)
        operation = self.writer.drain()
        if timeout is not None:
            await asyncio.wait_for(operation, timeout)
        else:
            await operation

    async def aclose(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                        timeout: float | None = None):
        await self.writer.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            ssl_handshake_timeout=timeout,
        )
        return self

    def get_extra_info(self, info: str):
        return self.writer.get_extra_info(info)


class _PoolStream(httpx.AsyncByteStream):
    def __init__(self, pool: httpcore.AsyncConnectionPool, stream):
        self.pool = pool
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        stream_error: BaseException | None = None
        try:
            await self.stream.aclose()
        except BaseException as exc:
            stream_error = exc
        try:
            await self.pool.aclose()
        except BaseException:
            if stream_error is None:
                raise
        if stream_error is not None:
            raise stream_error

    async def cleanup_after_close_failure(self) -> None:
        """Retry stream close and explicitly close its pool after a failed close."""
        try:
            await self.stream.aclose()
        except BaseException:
            pass
        await self.pool.aclose()
