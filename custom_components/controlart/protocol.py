"""Camada de protocolo TCP para os módulos cabeados ControlArt.

Os módulos MD-ETH-MCRL2 / MD-ETH-MCDM2 expõem um servidor TCP. Comandos são
strings ASCII terminadas em ``\\r\\n``. As respostas (``setcmd``, ``setdmmd``,
``setbmcb0md`` ...) e os eventos de keypad (``setcankpfb``) chegam pela mesma
conexão, então a integração mantém um socket persistente com reconexão
automática e um laço de leitura que despacha cada linha recebida.

Quedas de rede sem FIN/RST (cabo desconectado, switch desligado, módulo sem
energia) deixam o socket "meio aberto": sem ajustes, o sistema operacional
continua achando que está conectado e fica retransmitindo os comandos já
escritos, que acabam executados quando o módulo volta. Para evitar isso a
conexão usa keepalive e TCP_USER_TIMEOUT curtos e, ao cair, é fechada com RST
(SO_LINGER 0), descartando o que ainda não foi entregue. Comandos enviados sem
conexão são recusados — nada fica enfileirado para depois.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

_TERMINATOR = "\r\n"
_RECONNECT_DELAYS = (1, 2, 5, 10, 20, 30)
_READ_CHUNK = 1024
_CONNECT_TIMEOUT = 5

# Keepalive: detecta a queda do link mesmo sem tráfego (~10 s ocioso + sondas).
_KEEPALIVE_IDLE = 10
_KEEPALIVE_INTERVAL = 5
_KEEPALIVE_COUNT = 3
# Tempo máximo que um comando pode ficar sem confirmação TCP do módulo antes de
# o kernel derrubar a conexão e descartá-lo (Linux).
_TCP_USER_TIMEOUT_MS = 5000


class ControlArtNotConnected(ConnectionError):
    """Comando recusado porque não há conexão ativa com o módulo."""


class ControlArtProtocol:
    """Mantém uma conexão TCP persistente com um módulo ControlArt."""

    def __init__(
        self,
        host: str,
        port: int,
        message_callback: Callable[[str], None] | None = None,
        connect_callback: Callable[[], None] | None = None,
        disconnect_callback: Callable[[], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._message_callback = message_callback
        self._connect_callback = connect_callback
        self._disconnect_callback = disconnect_callback
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._run_task: asyncio.Task | None = None
        self._closing = False
        self._connected = asyncio.Event()
        self._waiters: list[tuple[Callable[[str], bool], asyncio.Future]] = []
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """True quando o socket está conectado."""
        return self._connected.is_set()

    @property
    def host(self) -> str:
        return self._host

    # ------------------------------------------------------------------ ciclo
    async def async_start(self) -> None:
        """Inicia o supervisor e aguarda a primeira conexão."""
        self._closing = False
        self._run_task = asyncio.create_task(self._supervisor())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=10)
        except asyncio.TimeoutError as err:
            await self.async_stop()
            raise ConnectionError(
                f"Sem resposta de {self._host}:{self._port}"
            ) from err

    async def async_stop(self) -> None:
        """Encerra a conexão e cancela o supervisor."""
        self._closing = True
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        await self._close_socket()
        for _pred, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()

    def drop_connection(self) -> None:
        """Derruba a conexão atual descartando comandos ainda não entregues.

        Usado quando o módulo para de responder com o socket ainda aberto.
        O supervisor detecta o fechamento e reconecta em seguida.
        """
        if self._writer is None:
            return
        _LOGGER.debug("Derrubando conexão sem resposta com %s", self._host)
        self._connected.clear()
        _abort_writer(self._writer)

    async def _supervisor(self) -> None:
        """Conecta, lê e reconecta com backoff até o encerramento."""
        attempt = 0
        while not self._closing:
            established = False
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=_CONNECT_TIMEOUT,
                )
                self._configure_socket()
                attempt = 0
                established = True
                self._connected.set()
                _LOGGER.debug("Conectado a %s:%s", self._host, self._port)
                if self._connect_callback is not None:
                    try:
                        self._connect_callback()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Erro no connect_callback")
                await self._reader_loop()
            except (
                OSError,
                ConnectionError,
                asyncio.IncompleteReadError,
                asyncio.TimeoutError,
            ) as err:
                _LOGGER.debug("Conexão com %s caiu: %s", self._host, err)
            finally:
                self._connected.clear()
                # Na queda, fecha com RST para o kernel não seguir tentando
                # entregar comandos pendentes quando o módulo voltar.
                await self._close_socket(abort=not self._closing)
                if established and not self._closing:
                    self._fail_waiters()
                    if self._disconnect_callback is not None:
                        try:
                            self._disconnect_callback()
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("Erro no disconnect_callback")
            if self._closing:
                break
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            _LOGGER.debug("Reconectando a %s em %ss", self._host, delay)
            await asyncio.sleep(delay)

    def _configure_socket(self) -> None:
        """Ativa keepalive e TCP_USER_TIMEOUT para detectar quedas do link."""
        assert self._writer is not None
        sock = self._writer.get_extra_info("socket")
        if sock is None:
            return
        options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        for name, value in (
            ("TCP_KEEPIDLE", _KEEPALIVE_IDLE),
            ("TCP_KEEPINTVL", _KEEPALIVE_INTERVAL),
            ("TCP_KEEPCNT", _KEEPALIVE_COUNT),
            ("TCP_USER_TIMEOUT", _TCP_USER_TIMEOUT_MS),
        ):
            opt = getattr(socket, name, None)
            if opt is not None:
                options.append((socket.IPPROTO_TCP, opt, value))
        for level, opt, value in options:
            try:
                sock.setsockopt(level, opt, value)
            except OSError as err:
                _LOGGER.debug("setsockopt %s falhou: %s", opt, err)

    async def _reader_loop(self) -> None:
        """Lê o stream, separa linhas por ``\\n`` e despacha."""
        assert self._reader is not None
        buffer = ""
        while not self._closing:
            data = await self._reader.read(_READ_CHUNK)
            if not data:
                raise ConnectionError("Conexão encerrada pelo módulo")
            buffer += data.decode("ascii", errors="ignore")
            while "\n" in buffer:
                raw, buffer = buffer.split("\n", 1)
                line = raw.strip("\r").strip()
                if line:
                    self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        _LOGGER.debug("RX %s: %s", self._host, line)
        for pred, fut in list(self._waiters):
            if not fut.done() and pred(line):
                fut.set_result(line)
                if (pred, fut) in self._waiters:
                    self._waiters.remove((pred, fut))
        if self._message_callback is not None:
            try:
                self._message_callback(line)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Erro processando linha: %s", line)

    def _fail_waiters(self) -> None:
        """Libera na hora as consultas pendentes de uma conexão que caiu."""
        for _pred, fut in self._waiters:
            if not fut.done():
                fut.set_exception(
                    ControlArtNotConnected(f"Conexão com {self._host} perdida")
                )

    # --------------------------------------------------------------- comandos
    async def async_send(self, command: str) -> None:
        """Envia um comando pela conexão atual.

        Levanta ``ControlArtNotConnected`` se não houver conexão: o comando é
        descartado, nunca guardado para quando o módulo voltar.
        """
        writer = self._writer
        if not self.connected or writer is None:
            raise ControlArtNotConnected(
                f"Sem conexão com {self._host}; comando descartado: {command}"
            )
        async with self._write_lock:
            # A conexão pode ter caído (ou sido refeita) enquanto aguardava.
            if not self.connected or self._writer is not writer:
                raise ControlArtNotConnected(
                    f"Conexão com {self._host} caiu; comando descartado: {command}"
                )
            _LOGGER.debug("TX %s: %s", self._host, command)
            writer.write((command + _TERMINATOR).encode("ascii"))
            await writer.drain()

    async def async_query(
        self,
        command: str,
        predicate: Callable[[str], bool],
        timeout: float = 5.0,
    ) -> str:
        """Envia um comando e aguarda a primeira linha que satisfaça ``predicate``."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.append((predicate, fut))
        try:
            await self.async_send(command)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            for entry in list(self._waiters):
                if entry[1] is fut:
                    self._waiters.remove(entry)

    async def _close_socket(self, abort: bool = False) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        if abort:
            _abort_writer(writer)
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass


def _abort_writer(writer: asyncio.StreamWriter) -> None:
    """Fecha o socket com RST, descartando o buffer de envio.

    Um ``close()`` comum deixa o kernel tentando entregar os bytes pendentes;
    com SO_LINGER 0 eles são descartados na hora.
    """
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        except OSError:
            pass
    writer.transport.abort()
