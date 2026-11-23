"""辅助流写端：正常排空，显式取消唤醒网络和广播队列，句柄由单一 socket 所有者释放。"""
from __future__ import annotations

from collections import deque
import errno
import io
from multiprocessing.connection import Connection, answer_challenge, deliver_challenge
import select
import socket
import threading
import time

from flowocr.extract import framesource


class OwnedSocket:
    """One IO owner; another thread may signal abort before/during connect or IO."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.sock = None

    def check(self):
        if self.cancelled.is_set():
            raise OSError('aux sender explicitly aborted')

    def connect(self, address, timeout=60):
        with self.lock:
            self.check()
            self.sock = sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        result = sock.connect_ex(address)
        pending = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035, 10036, 10037}
        deadline = time.monotonic() + timeout
        while result:
            self.check()
            if result not in pending:
                raise OSError(result, 'aux socket connect failed')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('aux socket connect timed out')
            try:
                _, writable, errors = select.select([], [sock], [sock], min(.05, remaining))
            except ValueError as exc:
                # abort() detached the socket between check() and select(): fd is -1.
                # Report it as the abort it is, so callers only handle OSError.
                raise OSError('aux sender aborted during connect') from exc
            if writable or errors:
                result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        self.check()
        sock.setblocking(True)
        return sock

    def abort(self):
        with self.lock:
            self.cancelled.set()
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                handle = self.sock.detach()
                self.sock = None
                if handle >= 0:
                    socket.close(handle)

    def dispose(self):
        # Only the IO owner calls this, or an aborter holding its operation lock.
        with self.lock:
            sock, self.sock = self.sock, None
            if sock is not None:
                sock.close()


class SocketConnection(Connection):
    """Keep standard MP framing/pickling/auth; IO uses an invalidatable socket.

    Connection borrows fileno solely for its open/closed check. Every blocking IO
    uses the socket object, and _close never closes that borrowed handle. No dup:
    a duplicate kept Windows recv alive after closing the original in a probe.
"""

    def __init__(self, sock):
        super().__init__(sock.fileno())
        self.sock = sock

    def _close(self):
        pass  # OwnedSocket alone releases this handle; never close a reused one.

    def fileno(self):
        self._check_closed()
        return self.sock.fileno()  # never expose a stale borrowed numeric handle

    def _send(self, buf):
        self.sock.sendall(buf)

    def _recv(self, size):
        result = io.BytesIO()
        remaining = size
        while remaining:
            buf = self.sock.recv(remaining)
            if not buf:
                if remaining == size:
                    raise EOFError
                raise OSError('got end of file during message')
            result.write(buf)
            remaining -= len(buf)
        return result


class AuxRemote:
    """Lazy, cancellable AF_INET MP feed; standard authentication and p/pe wire."""

    def __init__(self, cfg):
        self.url = cfg['url']
        port, key = cfg['pts_addr']
        self.address, self.key = ('127.0.0.1', int(port)), bytes.fromhex(key)
        self.owner = OwnedSocket()
        self.io = threading.Lock()
        self.life = threading.Lock()
        self.conn = None
        self.finished = False

    def _dispose(self):
        with self.life:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
        self.owner.dispose()

    def pts_put(self, item):
        with self.io:
            try:
                self.owner.check()
                if self.finished:
                    raise OSError('aux PTS feed already closed')
                if self.conn is None:
                    sock = self.owner.connect(self.address)
                    conn = SocketConnection(sock)
                    with self.life:
                        self.owner.check()
                        self.conn = conn
                    answer_challenge(conn, self.key)
                    deliver_challenge(conn, self.key)
                else:
                    conn = self.conn
                self.owner.check()
                conn.send(('pe',) if item is framesource.QUEUE_SENTINEL else ('p', item))
                self.owner.check()
                if item is framesource.QUEUE_SENTINEL:
                    self.finished = True
                    self._dispose()
            except BaseException:
                self.finished = True
                self._dispose()
                raise

    def abort(self):
        self.owner.abort()
        # The adapter's close only invalidates a borrowed handle; no raw handle
        # is closed by Connection, even if send/recv has just returned. Complete
        # this cleanup here rather than hoping the IO owner will see abort later.
        with self.life:
            self.finished = True
            if self.conn is not None:
                self.conn.close()
                self.conn = None


class TcpAux:
    """Registered before connect; abort never waits for send's operation lock."""

    def __init__(self, remote):
        self.remote = remote
        self.owner = OwnedSocket()
        self.io = threading.Lock()
        self.opened = False
        self.finished = False

    def open(self):
        with self.io:
            if self.opened or self.finished:
                raise OSError('aux pixel writer open is single-use')
            try:
                self.owner.check()
                host, port = self.remote.url.removeprefix('tcp://').rsplit(':', 1)
                self.owner.connect((host, int(port)))
                self.opened = True
                return self
            except BaseException:
                self.owner.dispose()
                self.remote.abort()
                raise

    def send(self, frame, n, t):
        with self.io:
            try:
                self.owner.check()
                if not self.opened or self.finished:
                    raise OSError('aux pixel writer is not open')
                self.owner.sock.sendall(memoryview(frame).cast('B'))
                self.owner.check()
                self.remote.pts_put((n, t))
            except BaseException:
                self.owner.dispose()
                self.remote.abort()
                self.finished = True
                raise

    def close(self):
        with self.io:
            if self.finished:
                return
            try:
                self.owner.check()
                self.remote.pts_put(framesource.QUEUE_SENTINEL)
            finally:
                self.finished = True
                self.owner.dispose()

    def abort(self):
        self.owner.abort()
        self.remote.abort()
        self.finished = True


class AuxFanout:
    """Independent bounded queues, no queue sentinel competing for capacity."""

    def __init__(self, remotes, depth=512):
        if depth <= 0:
            raise ValueError('fanout depth must be positive')
        self.depth = depth
        self.cv = threading.Condition()
        self.qs = [deque() for _ in remotes]
        self.writers = [TcpAux(r) for r in remotes]
        self.dead = [False] * len(remotes)
        self.errors = []
        self.closing = False
        self.cancelled = False
        self.started = False
        self.sending = 0
        self.waiting_peer = None
        self.send_io = threading.Lock()
        self.ths = [threading.Thread(target=self._pump, args=(i,), daemon=True,
                    name=f'aux-fan-{i}') for i in range(len(remotes))]

    def open(self):
        with self.cv:
            if self.started or self.closing or self.cancelled:
                raise RuntimeError('fanout open is single-use')
            self.started = True
            for th in self.ths:
                th.start()
        return self

    def _pump(self, i):
        writer = self.writers[i]
        try:
            writer.open()
            while True:
                with self.cv:
                    while not self.qs[i] and not self.dead[i] and (not self.closing or self.sending):
                        self.cv.wait()
                    if self.dead[i]:
                        return
                    if not self.qs[i]:
                        break
                    item = self.qs[i].popleft()
                    self.cv.notify_all()
                writer.send(*item)
            writer.close()  # EOF follows every admitted frame on normal close.
        except BaseException as exc:
            with self.cv:
                if not self.dead[i]:
                    self.errors.append(f'subscriber {i}: {exc!r}')
                self.dead[i] = True
                self.qs[i].clear()
                self.cv.notify_all()
        finally:
            writer.abort()  # normal close already disposed; failure cannot block.

    def send(self, frame, n, t):
        item = (bytes(memoryview(frame).cast('B')), n, t)
        with self.send_io:
            with self.cv:
                if not self.started or self.closing or self.cancelled:
                    raise OSError('fanout is not accepting frames')
                self.sending += 1
                try:
                    for i, q in enumerate(self.qs):
                        while len(q) >= self.depth and not self.dead[i] and not self.cancelled:
                            self.waiting_peer = i
                            try:
                                self.cv.wait()
                            finally:
                                self.waiting_peer = None
                        if self.cancelled:
                            raise OSError('fanout aborted during send')
                        if not self.dead[i]:
                            q.append(item)
                            self.cv.notify_all()
                finally:
                    self.sending -= 1
                    self.cv.notify_all()

    def abort(self, peer=None):
        with self.cv:
            if peer is None:
                self.cancelled = True
            targets = range(len(self.writers)) if peer is None else (peer,)
            for i in targets:
                self.dead[i] = True
                self.qs[i].clear()
            self.cv.notify_all()
        for i in targets:
            self.writers[i].abort()

    def close(self, timeout=None):
        with self.cv:
            self.closing = True
            self.cv.notify_all()
            started = self.started
        if not started:
            self.abort()
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        for th in self.ths:
            th.join(None if deadline is None else max(0, deadline - time.monotonic()))
        if any(th.is_alive() for th in self.ths):
            raise TimeoutError('fanout normal drain not finished; explicit abort required')
