"""Coordinator dos módulos cabeados ControlArt.

Mantém o estado do módulo (entradas, saídas, canais motor, rampas, mín/máx),
constrói/envia comandos e processa as linhas recebidas pela conexão TCP —
tanto respostas a comandos quanto atualizações espontâneas (teclados físicos,
keypads CAN Bus, etc.).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MAC_BYTES,
    CONF_MODULE_TYPE,
    DEFAULT_PORT,
    DIMMER_OUTPUT_COUNT,
    DOMAIN,
    INPUT_COUNT,
    KEYPAD_EVT_MAP,
    MODULE_DIMMER,
    MODULE_RELAY,
    MOTOR_CHANNEL_COUNT,
    OPT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    RELAY_OUTPUT_COUNT,
    SIGNAL_KEYPAD,
)
from .protocol import ControlArtProtocol

_LOGGER = logging.getLogger(__name__)


@dataclass
class MotorState:
    """Estado de um canal motor/cortina/persiana."""

    out_up: int = 0
    out_down: int = 0
    position: int | None = None  # 0..255 (255 = fechada); None = sem leitura


@dataclass
class ModuleState:
    """Snapshot do estado de um módulo."""

    inputs: list[int] = field(default_factory=lambda: [0] * INPUT_COUNT)
    outputs: list[int] = field(default_factory=list)
    motors: dict[int, MotorState] = field(default_factory=dict)
    ramps: list[int] = field(default_factory=lambda: [0] * DIMMER_OUTPUT_COUNT)
    minmax: list[tuple[int, int]] = field(
        default_factory=lambda: [(0, 255)] * DIMMER_OUTPUT_COUNT
    )


class ControlArtCoordinator(DataUpdateCoordinator[ModuleState]):
    """Gerencia a comunicação com um módulo cabeado."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.module_type: str = entry.data[CONF_MODULE_TYPE]
        self.mac_bytes: list[int] = list(entry.data[CONF_MAC_BYTES])
        out_count = (
            RELAY_OUTPUT_COUNT
            if self.module_type == MODULE_RELAY
            else DIMMER_OUTPUT_COUNT
        )
        self.state = ModuleState(outputs=[0] * out_count)
        self._ping_results: dict[str, int] | None = None

        scan = entry.options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        host = entry.data["host"]
        port = entry.data.get("port", DEFAULT_PORT)
        self.protocol = ControlArtProtocol(
            host,
            port,
            message_callback=self._handle_message,
            connect_callback=self._on_connect,
            disconnect_callback=self._on_disconnect,
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=scan),
        )

    # ------------------------------------------------------------- utilidades
    @property
    def mac_str(self) -> str:
        """MAC no formato decimal para os comandos (ex.: '75,59,100')."""
        return ",".join(str(b) for b in self.mac_bytes)

    def _status_prefix(self) -> str:
        return "setcmd" if self.module_type == MODULE_RELAY else "setdmmd"

    # --------------------------------------------------------------- ciclo HA
    async def async_setup(self) -> None:
        """Conecta ao módulo e faz a primeira leitura completa."""
        await self.protocol.async_start()
        if self.module_type == MODULE_DIMMER:
            await self._fetch_dimmer_config()
        await self.async_config_entry_first_refresh()

    async def async_shutdown(self) -> None:
        await self.protocol.async_stop()
        await super().async_shutdown()

    @callback
    def _on_connect(self) -> None:
        """Após (re)conectar, agenda uma atualização.

        As entidades só voltam a ficar disponíveis quando o módulo responder
        ao status. Chama o refresh direto (sem o debouncer) para não atrasar
        a volta em até 10 s após uma reconexão rápida.
        """
        self.hass.async_create_task(self.async_refresh())

    @callback
    def _on_disconnect(self) -> None:
        """Conexão caiu: marca as entidades como indisponíveis na hora."""
        self.async_set_update_error(
            UpdateFailed(f"Conexão com {self.protocol.host} perdida")
        )

    async def _async_update_data(self) -> ModuleState:
        """Solicita o status completo do módulo."""
        cmd = f"mdcmd_getmd,{self.mac_str}"
        prefix = self._status_prefix()
        try:
            await self.protocol.async_query(
                cmd, lambda line: line.startswith(prefix), timeout=6
            )
        except ConnectionError as err:
            raise UpdateFailed("Módulo desconectado") from err
        except asyncio.TimeoutError as err:
            # Socket aberto mas sem resposta: provável queda de rede sem
            # FIN/RST. Derruba a conexão para descartar comandos pendentes e
            # deixa o supervisor reconectar.
            self.protocol.drop_connection()
            raise UpdateFailed("Módulo não respondeu ao status") from err
        return self.state

    async def _fetch_dimmer_config(self) -> None:
        """Lê rampas e valores mín/máx (somente módulo dimmer)."""
        for cmd, prefix in (
            (f"mdcmd_getrampmd,{self.mac_str}", "setrampmd"),
            (f"mdcmd_getminmaxmd,{self.mac_str}", "setminmaxmd"),
        ):
            try:
                await self.protocol.async_query(
                    cmd, lambda line, p=prefix: line.startswith(p), timeout=5
                )
            except (asyncio.TimeoutError, ConnectionError):
                _LOGGER.debug("Sem resposta para %s", cmd)

    # ------------------------------------------------------- recepção/parsing
    @callback
    def _handle_message(self, line: str) -> None:
        """Processa uma linha recebida pela conexão TCP."""
        try:
            self._parse_line(line)
        except (ValueError, IndexError):
            _LOGGER.debug("Linha ignorada (formato inesperado): %s", line)

    def _parse_line(self, line: str) -> None:
        updated = False

        if line.startswith("setcmd,"):
            parts = line.split(",")
            values = [int(v) for v in parts[2:]]
            self.state.inputs = values[:INPUT_COUNT]
            self.state.outputs = values[INPUT_COUNT : INPUT_COUNT + RELAY_OUTPUT_COUNT]
            updated = True

        elif line.startswith("setdmmd,"):
            parts = line.split(",")
            values = [int(v) for v in parts[2:]]
            self.state.inputs = values[:INPUT_COUNT]
            self.state.outputs = values[
                INPUT_COUNT : INPUT_COUNT + DIMMER_OUTPUT_COUNT
            ]
            updated = True

        elif line.startswith("setbmcb0md,"):
            parts = line.split(",")
            ch = int(parts[2])
            self.state.motors[ch] = MotorState(
                out_up=int(parts[3]),
                out_down=int(parts[4]),
                position=int(parts[5]),
            )
            updated = True

        elif line.startswith("setrampmd,"):
            parts = line.split(",")
            self.state.ramps = [int(v) for v in parts[2 : 2 + DIMMER_OUTPUT_COUNT]]
            updated = True

        elif line.startswith("setminmaxmd,"):
            parts = line.split(",")
            vals = [int(v) for v in parts[2:]]
            pairs = [
                (vals[i], vals[i + 1])
                for i in range(0, min(len(vals), DIMMER_OUTPUT_COUNT * 2), 2)
            ]
            if pairs:
                self.state.minmax = pairs
            updated = True

        elif line.startswith("setcankpfb,"):
            self._handle_keypad_event(line)

        elif line.startswith("CA_WN") and "PING_RPY" in line:
            self._handle_ping_reply(line)

        if updated:
            self.async_set_updated_data(self.state)

    def _handle_keypad_event(self, line: str) -> None:
        """setcankpfb,TYP_ID,DEV_ID,EVT,KEY."""
        parts = line.split(",")
        dev_id = parts[2].strip()
        evt = int(parts[3])
        key = int(parts[4])
        evt_name = KEYPAD_EVT_MAP.get(evt)
        if evt_name is None:
            return
        _LOGGER.debug("Keypad %s tecla %s -> %s", dev_id, key, evt_name)
        async_dispatcher_send(
            self.hass,
            f"{SIGNAL_KEYPAD}_{self.entry.entry_id}",
            dev_id,
            key,
            evt_name,
        )

    def _handle_ping_reply(self, line: str) -> None:
        """CA_WN:0 TYP_ID:01 DEV_ID:00-74-B1 CMD:PING_RPY."""
        if self._ping_results is None:
            return
        tokens = dict(t.split(":", 1) for t in line.split() if ":" in t)
        dev_id = tokens.get("DEV_ID")
        typ = tokens.get("TYP_ID")
        if dev_id and typ:
            self._ping_results[dev_id] = int(typ)

    # ------------------------------------------------------------- comandos
    async def _async_send(self, command: str) -> None:
        """Envia um comando; recusa se o módulo estiver indisponível.

        Evita que um comando dado com o módulo fora da rede seja executado
        horas depois, quando ele voltar.
        """
        if not self.last_update_success:
            raise HomeAssistantError(
                f"{self.entry.title} indisponível; comando não enviado"
            )
        try:
            await self.protocol.async_send(command)
        except ConnectionError as err:
            raise HomeAssistantError(
                f"{self.entry.title} desconectado; comando não enviado"
            ) from err

    # ------------------------------------------------------- comandos: relés
    async def async_set_relay(self, ch: int, value: int) -> None:
        """Liga/desliga uma saída de relé (CH 0..9, value 0/1)."""
        await self._async_send(
            f"mdcmd_sendrele,{self.mac_str},{ch},{value}"
        )

    async def async_set_relays(self, mask: int, value: int) -> None:
        """Liga/desliga múltiplas saídas de relé via máscara."""
        await self._async_send(
            f"mdcmd_msendrele,{self.mac_str},{mask},{value}"
        )

    async def async_toggle_relays(self, mask: int) -> None:
        """Inverte múltiplas saídas de relé via máscara."""
        await self._async_send(
            f"mdcmd_mtogglerele,{self.mac_str},{mask}"
        )

    # ----------------------------------------------------- comandos: dimmer
    async def async_set_dimmer_percent(
        self, ch: int, percent: int, ramp: int
    ) -> None:
        """Ajusta uma saída de dimmer por percentual (0..100), respeita mín/máx."""
        await self.async_set_dimmers_percent(1 << ch, percent, ramp)

    async def async_set_dimmers_percent(
        self, mask: int, percent: int, ramp: int
    ) -> None:
        """Ajusta múltiplas saídas de dimmer por percentual via máscara."""
        await self._async_send(
            f"mdcmd_msendpermd,{self.mac_str},{mask},{percent},{ramp}"
        )

    async def async_toggle_dimmers(self, mask: int) -> None:
        """Inverte múltiplas saídas de dimmer via máscara."""
        await self._async_send(
            f"mdcmd_mtogglemd,{self.mac_str},{mask}"
        )

    # --------------------------------------------- comandos: motor/cortina
    async def async_cover_move(self, ch: int, direction: int) -> None:
        """Aciona a cortina: direction 0=subir, 1=parar, 2=descer (FUNC=2)."""
        await self._async_send(
            f"mdcmd_sendcmd,{self.mac_str},2,{direction},{ch}"
        )

    async def async_cover_set_position(self, ch: int, position: int) -> None:
        """Move a cortina para uma posição absoluta do módulo (0..255, FUNC=0)."""
        await self._async_send(
            f"mdcmd_sendcmd,{self.mac_str},0,{position},{ch}"
        )

    async def async_calibrate(self, ch: int, action: str) -> None:
        """Comandos de calibração da cortina (operam no módulo local)."""
        cmd = {
            "start_up": f"mdcmd_startcalibupmd,{ch}",
            "stop_up": f"mdcmd_stopcalibupmd,{ch}",
            "start_down": f"mdcmd_startcalibdownmd,{ch}",
            "stop_down": f"mdcmd_stopcalibdownmd,{ch}",
            "reset": f"mdcmd_resetcalibdownmd,{ch}",
        }[action]
        await self._async_send(cmd)

    async def async_set_motor_mode(self, ch: int, mode: int) -> None:
        """Altera o modo de operação do motor (0=normal, 1=sem feedback)."""
        await self._async_send(
            f"mdcmd_setmotormodemd,{ch},{mode}"
        )

    # ----------------------------------------------------------- keypad scan
    async def async_scan_keypads(self, duration: float = 2.5) -> dict[str, int]:
        """Faz o SCAN da rede CAN Bus e retorna {dev_id: typ_id}."""
        self._ping_results = {}
        try:
            await self._async_send("can_ping_req,0,0x000000")
            await asyncio.sleep(duration)
            results = self._ping_results or {}
        finally:
            self._ping_results = None
        return dict(results)
