"""Integração ControlArt — módulos cabeados relé (MD-ETH-MCRL2) e dimmer
(MD-ETH-MCDM2)."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, PLATFORMS
from .coordinator import ControlArtCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura um módulo a partir de um config entry."""
    coordinator = ControlArtCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except ConfigEntryNotReady:
        # Primeira leitura falhou: encerra a conexão antes do HA tentar de novo.
        await coordinator.async_shutdown()
        raise
    except (ConnectionError, TimeoutError) as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Descarrega um config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: ControlArtCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recarrega a integração quando as opções mudam."""
    await hass.config_entries.async_reload(entry.entry_id)
