import errno
import socket
import ssl
from abc import ABCMeta, abstractmethod
from ipaddress import ip_address
from typing import Union, Tuple, Any, Optional, Callable, AsyncIterable

from async_generator import async_generator, yield_

from anyio import abc
from anyio.abc import IPAddressType, BufferType
from anyio.exceptions import DelimiterNotFound, IncompleteRead


class BaseSocket(metaclass=ABCMeta):
    __slots__ = '_raw_socket'

    def __init__(self, raw_socket: socket.SocketType) -> None:
        self._raw_socket = raw_socket
        self._raw_socket.setblocking(False)

    def __getattr__(self, item):
        return getattr(self._raw_socket, item)

    @abstractmethod
    async def _wait_readable(self) -> None:
        pass

    @abstractmethod
    async def _wait_writable(self) -> None:
        pass

    @abstractmethod
    async def _notify_close(self) -> None:
        pass

    @abstractmethod
    async def _check_cancelled(self) -> None:
        pass

    @abstractmethod
    async def _run_in_thread(self, func: Callable, *args):
        pass

    async def accept(self):
        await self._check_cancelled()
        try:
            raw_socket, address = self._raw_socket.accept()
        except BlockingIOError:
            await self._wait_readable()
            raw_socket, address = self._raw_socket.accept()

        return self.__class__(raw_socket), address

    async def bind(self, address: Union[Tuple[str, int], str]) -> None:
        await self._check_cancelled()
        if isinstance(address, tuple) and len(address) == 2:
            # For IP address/port combinations, call bind() directly
            try:
                ip_address(address[0])
            except ValueError:
                pass
            else:
                self._raw_socket.bind(address)
                return

        # In all other cases, do this in a worker thread to avoid blocking the event loop thread
        await self._run_in_thread(self._raw_socket.bind, address)

    async def close(self):
        await self._notify_close()
        self._raw_socket.close()

    async def connect(self, address: Union[tuple, str, bytes]) -> None:
        await self._check_cancelled()
        try:
            self._raw_socket.connect(address)
        except BlockingIOError:
            await self._wait_writable()

        error = self._raw_socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error:
            raise OSError(error, errno.errorcode[error])

    async def recv(self, size: int, *, flags: int = 0) -> bytes:
        while True:
            await self._check_cancelled()
            try:
                return self._raw_socket.recv(size, flags)
            except (BlockingIOError, ssl.SSLWantReadError):
                await self._wait_readable()
            except ssl.SSLWantWriteError:
                await self._wait_writable()

    async def recv_into(self, buffer, nbytes: int, *, flags: int = 0) -> int:
        while True:
            await self._check_cancelled()
            try:
                return self._raw_socket.recv_into(buffer, nbytes, flags)
            except (BlockingIOError, ssl.SSLWantReadError):
                await self._wait_readable()
            except ssl.SSLWantWriteError:
                await self._wait_writable()

    async def recvfrom(self, size: int, *, flags: int = 0) -> Tuple[bytes, Any]:
        await self._check_cancelled()
        try:
            return self._raw_socket.recvfrom(size, flags)
        except BlockingIOError:
            await self._wait_readable()
            return self._raw_socket.recvfrom(size, flags)

    async def recvfrom_into(self, buffer, size: int, *, flags: int = 0):
        await self._check_cancelled()
        try:
            return self._raw_socket.recvfrom_into(buffer, size, flags)
        except BlockingIOError:
            await self._wait_readable()
            return self._raw_socket.recvfrom_into(buffer, size, flags)

    async def send(self, data: bytes, *, flags: int = 0) -> int:
        while True:
            await self._check_cancelled()
            try:
                return self._raw_socket.send(data, flags)
            except (BlockingIOError, ssl.SSLWantWriteError):
                await self._wait_writable()
            except ssl.SSLWantReadError:
                await self._wait_readable()

    async def sendto(self, data: bytes, addr, *, flags: int = 0) -> int:
        await self._check_cancelled()
        try:
            return self._raw_socket.sendto(data, flags, addr)
        except BlockingIOError:
            await self._wait_writable()
            return self._raw_socket.sendto(data, flags, addr)

    async def sendall(self, data: bytes, *, flags: int = 0) -> None:
        to_send = len(data)
        while to_send > 0:
            await self._check_cancelled()
            try:
                sent = self._raw_socket.send(data, flags)
            except (BlockingIOError, ssl.SSLWantWriteError):
                await self._wait_writable()
            except ssl.SSLWantReadError:
                await self._wait_readable()
            else:
                to_send -= sent

    async def start_tls(self, context: ssl.SSLContext,
                        server_hostname: Optional[str] = None) -> None:
        plain_socket = self._raw_socket
        self._raw_socket = context.wrap_socket(
            self._raw_socket, server_side=not server_hostname, do_handshake_on_connect=False,
            server_hostname=server_hostname)
        while True:
            try:
                self._raw_socket.do_handshake()
                return
            except ssl.SSLWantReadError:
                await self._wait_readable()
            except ssl.SSLWantWriteError:
                await self._wait_writable()
            except BaseException:
                self._raw_socket = plain_socket
                raise


class SocketStream(abc.SocketStream):
    __slots__ = '_socket', '_ssl_context', '_server_hostname'

    def __init__(self, sock: BaseSocket, ssl_context: Optional[ssl.SSLContext] = None,
                 server_hostname: Optional[str] = None) -> None:
        self._socket = sock
        self._ssl_context = ssl_context
        self._server_hostname = server_hostname

    async def close(self):
        await self._socket.close()

    async def receive_some(self, max_bytes: Optional[int]) -> bytes:
        return await self._socket.recv(max_bytes)

    async def receive_exactly(self, nbytes: int) -> bytes:
        buf = bytearray(nbytes)
        view = memoryview(buf)
        while nbytes > 0:
            bytes_read = await self._socket.recv_into(view, nbytes)
            if bytes_read == 0:
                total_bytes_read = len(buf) - nbytes
                raise IncompleteRead(buf[:total_bytes_read])

            view = view[bytes_read:]
            nbytes -= bytes_read

        return bytes(buf)

    async def receive_until(self, delimiter: bytes, max_size: int) -> bytes:
        offset = 0
        delimiter_size = len(delimiter)
        buf = b''
        while len(buf) < max_size:
            read_size = max_size - len(buf)
            data = await self._socket.recv(read_size, flags=socket.MSG_PEEK)
            if data == b'':
                raise IncompleteRead(buf)

            buf += data
            index = buf.find(delimiter, offset)
            if index >= 0:
                await self._socket.recv(index + len(delimiter))
                return buf[:index]
            else:
                await self._socket.recv(len(data))
                offset += len(data) - delimiter_size + 1

        raise DelimiterNotFound(buf)

    @async_generator
    async def receive_chunks(self, max_size: int) -> AsyncIterable[bytes]:
        while True:
            data = await self.receive_some(max_size)
            if data:
                await yield_(data)
            else:
                break

    @async_generator
    async def receive_delimited_chunks(self, delimiter: bytes,
                                       max_chunk_size: int) -> AsyncIterable[bytes]:
        while True:
            try:
                chunk = await self.receive_until(delimiter, max_chunk_size)
            except IncompleteRead as exc:
                if exc.data:
                    raise
                else:
                    return

            await yield_(chunk)

    async def send_all(self, data: BufferType) -> None:
        return await self._socket.sendall(data)

    async def start_tls(self, context: Optional[ssl.SSLContext] = None) -> None:
        ssl_context = context or self._ssl_context or ssl.create_default_context()
        await self._socket.start_tls(ssl_context, self._server_hostname)


class SocketStreamServer(abc.SocketStreamServer):
    __slots__ = '_socket', '_ssl_context'

    def __init__(self, sock: BaseSocket, ssl_context: Optional[ssl.SSLContext]) -> None:
        self._socket = sock
        self._ssl_context = ssl_context

    async def close(self) -> None:
        await self._socket.close()

    @property
    def address(self) -> Union[tuple, str]:
        return self._socket.getsockname()

    async def accept(self):
        sock, addr = await self._socket.accept()
        try:
            stream = SocketStream(sock)
            if self._ssl_context:
                await stream.start_tls(self._ssl_context)

            return stream
        except BaseException:
            await sock.close()
            raise

    @async_generator
    async def accept_connections(self):
        while not self._socket.closed:
            await yield_(await self.accept())


class DatagramSocket(abc.DatagramSocket):
    __slots__ = '_socket'

    def __init__(self, sock: BaseSocket) -> None:
        self._socket = sock

    async def close(self):
        await self._socket.close()

    @property
    def address(self) -> Union[Tuple[str, int], str]:
        return self._socket.getsockname()

    async def receive(self, max_bytes: int) -> Tuple[bytes, str]:
        return await self._socket.recvfrom(max_bytes)

    @async_generator
    async def receive_packets(self, max_size: int):
        while self._socket.fileno() != -1:
            packet, address = await self.receive(max_size)
            if packet:
                await yield_((packet, address))
            else:
                break

    async def send(self, data: bytes, address: Optional[IPAddressType] = None,
                   port: Optional[int] = None) -> None:
        if address is not None and port is not None:
            await self._socket.sendto(data, (str(address), port))
        else:
            await self._socket.send(data)