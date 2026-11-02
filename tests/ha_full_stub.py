"""Wider Home Assistant stub than ha_stub.py, for the HA-facing modules.

config_flow.py, coordinator.py and __init__.py import far more of HA than
state.py does. This stubs just enough of that surface (plus voluptuous and
bleak) to load the real package and exercise its logic with bare ``python3``,
in keeping with the rest of the suite.
"""

import enum
import importlib.util
import os
import sys
import types

import ha_stub

BASE = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "hidratespark_bluetooth_proxy"
)


def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, m)
    return m


# ------------------------------------------------------------------ bluetooth


class BluetoothCallbackMatcher(dict):
    def __init__(self, **kw):
        super().__init__(**kw)


class ServiceInfo:
    """Stand-in for BluetoothServiceInfoBleak."""

    def __init__(self, name, address, rssi=-60, connectable=True, service_uuids=()):
        self.name = name
        self.address = address
        self.rssi = rssi
        self.connectable = connectable
        self.service_uuids = list(service_uuids)
        self.device = types.SimpleNamespace(address=address, name=name)


class FakeBluetooth:
    """What the bluetooth stub serves; tests mutate it."""

    def __init__(self):
        self.by_address = {}  # address -> BLEDevice
        self.discovered = []  # list[ServiceInfo]
        self.callbacks = []  # (callback, matcher)


BT = FakeBluetooth()


# --------------------------------------------------------------- config flow


class ConfigFlow:
    def __init_subclass__(cls, domain=None, **kw):
        super().__init_subclass__(**kw)
        cls.DOMAIN = domain

    # Class-level defaults: like HA, flows needn't call super().__init__().
    unique_id = None
    abort_updates = None
    current_entries = ()

    @property
    def context(self):
        if "_context" not in self.__dict__:
            self._context = {}
        return self._context

    async def async_set_unique_id(self, uid):
        self.unique_id = uid

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        self.abort_updates = updates
        self.reload_on_update = reload_on_update

    def _async_current_entries(self):
        return self.current_entries

    def async_abort(self, reason):
        return {"type": "abort", "reason": reason}

    def async_show_form(self, **kw):
        return {"type": "form", **kw}

    def async_create_entry(self, **kw):
        return {"type": "create_entry", **kw}


class OptionsFlow:
    """HA >= 2024.12: no constructor arguments; config_entry is injected."""


class ConfigEntry:
    def __init__(self, entry_id="e1", title="", data=None, options=None,
                 unique_id=None, version=1, minor_version=1):
        self.entry_id = entry_id
        self.title = title
        self.data = dict(data or {})
        self.options = dict(options or {})
        self.unique_id = unique_id
        self.version = version
        self.minor_version = minor_version


class FakeConfigEntries:
    def __init__(self):
        self.entries = []
        self.updates = []
        self.reloads = []

    def async_entries(self, domain=None):
        return list(self.entries)

    def async_update_entry(self, entry, **kw):
        self.updates.append(kw)
        for k, v in kw.items():
            setattr(entry, k, v)
        return True

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)


class FakeHass:
    def __init__(self):
        self.config_entries = FakeConfigEntries()
        self.data = {}


def install():
    ha_stub._install_ha_stubs()

    # bleak / bleak-retry-connector
    bleak = _mod("bleak")
    bleak.BleakClient = type("BleakClient", (), {})
    _mod("bleak.backends")
    _mod("bleak.backends.device").BLEDevice = type("BLEDevice", (), {})
    brc = _mod("bleak_retry_connector")
    brc.BleakClientWithServiceCache = type("BleakClientWithServiceCache", (), {})

    async def establish_connection(*a, **k):
        raise RuntimeError("not used in unit tests")

    brc.establish_connection = establish_connection

    # voluptuous: the schemas are built but never validated here.
    vol = _mod("voluptuous")
    vol.Schema = lambda *a, **k: a
    vol.Required = lambda key, default=None: key
    vol.All = lambda *a: a
    vol.Range = lambda **k: k
    vol.In = lambda c: c

    core = sys.modules["homeassistant.core"]
    core.callback = lambda f: f
    core.CALLBACK_TYPE = object

    _mod("homeassistant.components")
    bt = _mod("homeassistant.components.bluetooth")
    bt.BluetoothServiceInfoBleak = ServiceInfo
    bt.BluetoothCallbackMatcher = BluetoothCallbackMatcher
    bt.BluetoothScanningMode = types.SimpleNamespace(PASSIVE="passive")
    bt.BluetoothChange = types.SimpleNamespace(ADVERTISEMENT="advertisement")
    bt.async_discovered_service_info = lambda hass, connectable=True: list(BT.discovered)
    bt.async_ble_device_from_address = (
        lambda hass, address, connectable=True: BT.by_address.get(address)
    )

    def register(hass, cb, matcher, mode):
        BT.callbacks.append((cb, matcher))
        return lambda: BT.callbacks.remove((cb, matcher))

    bt.async_register_callback = register

    ce = _mod("homeassistant.config_entries")
    ce.ConfigFlow, ce.OptionsFlow, ce.ConfigEntry = ConfigFlow, OptionsFlow, ConfigEntry
    _mod("homeassistant.data_entry_flow").FlowResult = dict
    const = _mod("homeassistant.const")
    const.CONF_ADDRESS = "address"
    const.Platform = enum.Enum("Platform", {"BINARY_SENSOR": "binary_sensor", "SENSOR": "sensor"})

    _mod("homeassistant.helpers.config_validation").positive_int = int
    dr = _mod("homeassistant.helpers.device_registry")
    dr.CONNECTION_BLUETOOTH = "bluetooth"
    dr.async_get = lambda hass: types.SimpleNamespace(
        async_get_device=lambda identifiers: None
    )
    disp = _mod("homeassistant.helpers.dispatcher")
    disp.async_dispatcher_send = lambda hass, signal: None
    disp.async_dispatcher_connect = lambda hass, signal, cb: (lambda: None)


def load_package():
    """Import the real integration package (as `hsp`) on top of the stubs."""
    install()
    spec = importlib.util.spec_from_file_location(
        "hsp", os.path.join(BASE, "__init__.py"), submodule_search_locations=[BASE]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hsp"] = pkg
    spec.loader.exec_module(pkg)
    return pkg
