"""Config flow for the HidrateSpark integration.

Discovery sources:
  * Bluetooth — when HA's bluetooth stack (or an ESPHome proxy) sees a bottle
    advertising the HydroSync reference service or a `h2o*` local name, the
    user is offered a one-click "Configure" action.
  * Manual — the user picks from any HidrateSpark candidate currently
    advertising in range, or types a MAC address directly.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_DEVICE_KEY,
    CONF_LOCAL_NAME,
    CONF_NAME_PREFIX,
    CONF_SIZE_ML,
    DEFAULT_NAME_PREFIX,
    DEFAULT_SIZE_ML,
    DOMAIN,
    SERVICE_REF,
    SERVICE_USER,
)

_LOGGER = logging.getLogger(__name__)


def _looks_like_bottle(info: BluetoothServiceInfoBleak) -> bool:
    uuids = {u.lower() for u in info.service_uuids or []}
    if SERVICE_REF.lower() in uuids or SERVICE_USER.lower() in uuids:
        return True
    if info.name and info.name.lower().startswith(DEFAULT_NAME_PREFIX):
        return True
    return False


def stable_id(name: str | None, address: str) -> str:
    """The unique ID for a bottle: its advertised name, else its address.

    Only an `h2o*` name counts. Without a real advertised name HA reports the
    address itself as the name, which is exactly what must not be used.
    """
    if name and name.lower().startswith(DEFAULT_NAME_PREFIX):
        return name
    return address.upper()


class HidrateSparkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HidrateSpark."""

    VERSION = 1
    # 1.2: unique ID moved from the BLE address to the advertised name, and
    # entries gained a fixed device key (see async_migrate_entry).
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    # ----------------------------------------------------------- bluetooth flow

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a bottle discovered via the bluetooth integration."""
        if not _looks_like_bottle(discovery_info):
            return self.async_abort(reason="not_supported")
        uid = stable_id(discovery_info.name, discovery_info.address)
        if uid == discovery_info.address.upper():
            # No name in this advert, so nothing tells a known bottle on a
            # fresh address apart from a new one; offering it would invite a
            # duplicate. A later advert carrying the name re-triggers discovery
            # through the manifest's local_name matcher.
            return self.async_abort(reason="name_unavailable")
        await self.async_set_unique_id(uid)
        # Already set up under an older address: record the new one, but don't
        # reload. The running coordinator follows the bottle by name itself, and
        # a reload would tear down a session that may be connected right now.
        self._abort_if_unique_id_configured(
            updates={CONF_ADDRESS: discovery_info.address.upper()},
            reload_on_update=False,
        )
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name
                or self._discovery_info.address,
                data={
                    CONF_ADDRESS: self._discovery_info.address.upper(),
                    CONF_DEVICE_KEY: self._discovery_info.address.upper(),
                    CONF_LOCAL_NAME: self._discovery_info.name,
                    CONF_NAME_PREFIX: DEFAULT_NAME_PREFIX,
                },
                options={CONF_SIZE_ML: user_input.get(CONF_SIZE_ML, DEFAULT_SIZE_ML)},
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address,
            },
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SIZE_ML, default=DEFAULT_SIZE_ML): vol.All(
                        cv.positive_int, vol.Range(min=100, max=2000)
                    ),
                }
            ),
        )

    # ---------------------------------------------------------------- user flow

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a manually-initiated flow."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            info = self._discovered.get(address)
            await self.async_set_unique_id(
                stable_id(info.name if info else None, address)
            )
            self._abort_if_unique_id_configured(
                updates={CONF_ADDRESS: address}, reload_on_update=False
            )
            return self.async_create_entry(
                title=(info.name if info and info.name else address),
                data={
                    CONF_ADDRESS: address,
                    CONF_DEVICE_KEY: address,
                    CONF_LOCAL_NAME: info.name if info else None,
                    CONF_NAME_PREFIX: DEFAULT_NAME_PREFIX,
                },
                options={CONF_SIZE_ML: user_input.get(CONF_SIZE_ML, DEFAULT_SIZE_ML)},
            )

        # Build a picker from currently-advertising candidates.
        configured = set()
        for entry in self._async_current_entries():
            configured.add(entry.unique_id)
            configured.add(str(entry.data.get(CONF_ADDRESS, "")).upper())
        for info in async_discovered_service_info(self.hass):
            if not _looks_like_bottle(info):
                continue
            if (
                info.address.upper() in configured
                or stable_id(info.name, info.address) in configured
            ):
                continue
            self._discovered[info.address.upper()] = info

        if self._discovered:
            choices = {
                addr: f"{(info.name or addr)} ({addr})"
                for addr, info in self._discovered.items()
            }
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(choices),
                    vol.Required(CONF_SIZE_ML, default=DEFAULT_SIZE_ML): vol.All(
                        cv.positive_int, vol.Range(min=100, max=2000)
                    ),
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Required(CONF_SIZE_ML, default=DEFAULT_SIZE_ML): vol.All(
                        cv.positive_int, vol.Range(min=100, max=2000)
                    ),
                }
            )

        return self.async_show_form(step_id="user", data_schema=schema)

    # ----------------------------------------------------------------- options

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        # Since HA 2024.12 the flow manager injects `self.config_entry`;
        # passing it to the constructor raises TypeError (issue #9).
        return HidrateSparkOptionsFlow()


class HidrateSparkOptionsFlow(OptionsFlow):
    """Allow the bottle size to be tuned after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(
            CONF_SIZE_ML, self.config_entry.data.get(CONF_SIZE_ML, DEFAULT_SIZE_ML)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SIZE_ML, default=current): vol.All(
                        cv.positive_int, vol.Range(min=100, max=2000)
                    ),
                }
            ),
        )
