"""
nge100/controller.py

High-level controller for the Rohde & Schwarz NGE100 series DC power supply.

Operating model — the NGE100 is a multi-channel bench supply (NGE102: 2 ch,
NGE103: 3 ch), each channel with independent voltage/current setpoints, output
state, protection (OVP/OPP/Fuse) and EasyRamp soft-start. A master output
switch (OUTP:GEN) lets you bring up multiple armed channels at once.

Usage (headless):

    from nge100 import NGE100Controller

    with NGE100Controller(mode="simulation") as ps:

        # Per-channel apply: set voltage + current limit in one call
        ps.apply(1, 5.0, 0.5)
        ps.output_on(1)

        # Measurement
        meas = ps.measure_all(1)        # {'voltage': .., 'current': .., 'power': ..}

        # Protection
        ps.set_ovp(1, 5.5)              # set OVP @ 5.5 V and enable
        ps.set_fuse(1, True, delay_ms=100)

        # Coordinated bring-up across channels
        ps.select_channel(1, True)
        ps.select_channel(2, True)
        ps.master_output_on()
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .driver import (
    NGE100Driver,
    DEFAULT_RESOURCE,
    VMIN, VMAX, IMIN, IMAX, PMAX,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ChannelStatus:
    """Snapshot of one channel's setpoints, measurements, and protection state."""
    channel:         int   = 0
    voltage_set:     float = 0.0
    current_set:     float = 0.0
    output_on:       bool  = False
    selected:        bool  = False
    measured_voltage: float = 0.0
    measured_current: float = 0.0
    measured_power:   float = 0.0
    ovp_enabled:  bool  = False
    ovp_level:    float = 0.0
    ovp_mode:     str   = ""
    ovp_tripped:  bool  = False
    opp_enabled:  bool  = False
    opp_level:    float = 0.0
    opp_tripped:  bool  = False
    fuse_enabled: bool  = False
    fuse_delay_ms: float = 0.0
    fuse_tripped: bool  = False


@dataclass
class InstrumentStatus:
    """Snapshot of full instrument state."""
    connected:     bool = False
    idn:           Optional[str] = None
    num_channels:  int = 0
    master_on:     Optional[bool] = None
    channels:      dict[int, ChannelStatus] = field(default_factory=dict)
    timestamp:     float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class NGE100Controller:
    """
    High-level controller for the R&S NGE100 series.

    Parameters
    ----------
    resource : str
        VISA resource string ("USB0::0x0AAD::0x0197::...::INSTR",
        "TCPIP0::192.168.1.10::INSTR") OR a serial port ("COM3").
        Transport is auto-detected from the string.
    mode : str
        "hardware" or "simulation".
    """

    # ------------------------------------------------------------------
    # Plugin interface
    # ------------------------------------------------------------------
    MODULE_NAME  = "NGE100"
    DEVICE_NAME  = "Rohde & Schwarz NGE100 Power Supply"
    CONFIG_FIELDS = [
        {"key": "resource", "label": "Resource (VISA or COM port)", "type": "str",    "default": DEFAULT_RESOURCE},
        {"key": "mode",     "label": "Mode",                        "type": "choice", "default": "simulation",
         "choices": ["simulation", "hardware"]},
        {"key": "default_voltage", "label": "Default voltage (V)",  "type": "float",  "default": 5.0},
        {"key": "default_current", "label": "Default current (A)",  "type": "float",  "default": 0.5},
        {"key": "default_ovp",     "label": "Default OVP (V)",      "type": "float",  "default": 32.0},
        {"key": "fuse_enabled",    "label": "Enable fuse by default","type": "bool",  "default": False},
    ]
    DEFAULTS = {
        "resource":        DEFAULT_RESOURCE,
        "mode":            "simulation",
        "default_voltage": 5.0,
        "default_current": 0.5,
        "default_ovp":     32.0,
        "fuse_enabled":    False,
    }

    @staticmethod
    def test(config: dict) -> tuple[bool, str]:
        try:
            ctrl = NGE100Controller(
                resource=config.get("resource", DEFAULT_RESOURCE),
                mode=config.get("mode", "simulation"),
            )
            ctrl.connect()
            idn = ctrl.identify()
            ctrl.disconnect()
            return True, f"OK — {idn}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    @staticmethod
    def read(config: dict) -> dict:
        return {
            "resource": config.get("resource", ""),
            "mode":     config.get("mode", "simulation"),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, resource: str = DEFAULT_RESOURCE, mode: str = "simulation"):
        self._driver = NGE100Driver(resource=resource, mode=mode)

        # Defaults applied on connect (overridden by configure())
        self._default_voltage = 5.0
        self._default_current = 0.5
        self._default_ovp     = 32.0
        self._fuse_enabled    = False

        # Optional progress/status hook: fn(message: str)
        self.on_status: Optional[Callable[[str], None]] = None

    def connect(self):
        self._driver.connect()

    def disconnect(self):
        try:
            # Turn outputs off on disconnect for safety
            if self._driver.is_connected:
                for ch in range(1, self.num_channels + 1):
                    try:
                        self._driver.output_off(ch)
                    except Exception:
                        pass
        finally:
            self._driver.disconnect()

    def identify(self) -> str:
        if self._driver.mode == "simulation":
            return f"R&S NGE100 [simulation] @ {self._driver.resource}"
        return f"{self._driver.idn} [{self._driver.transport}] @ {self._driver.resource}"

    @property
    def driver(self) -> NGE100Driver:
        return self._driver

    @property
    def is_connected(self) -> bool:
        return self._driver.is_connected

    @property
    def num_channels(self) -> int:
        return self._driver.num_channels

    @property
    def idn(self) -> Optional[str]:
        return self._driver.idn

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(self,
                  default_voltage: float = 5.0,
                  default_current: float = 0.5,
                  default_ovp:     float = 32.0,
                  fuse_enabled:    bool  = False):
        """Set defaults applied by helpers like configure_channel()."""
        self._default_voltage = default_voltage
        self._default_current = default_current
        self._default_ovp     = default_ovp
        self._fuse_enabled    = fuse_enabled

    def configure_channel(self, channel: int,
                          voltage: Optional[float] = None,
                          current: Optional[float] = None,
                          ovp:     Optional[float] = None,
                          fuse:    Optional[bool]  = None) -> bool:
        """Apply voltage/current setpoints and protection for one channel."""
        v = self._default_voltage if voltage is None else voltage
        i = self._default_current if current is None else current
        ok = self._driver.apply(channel, v, i)

        if ovp is not None or self._default_ovp is not None:
            self._driver.set_ovp_level(channel, ovp if ovp is not None else self._default_ovp)
            self._driver.set_ovp_state(channel, True)

        if fuse is not None or self._fuse_enabled:
            self._driver.set_fuse_state(channel, fuse if fuse is not None else self._fuse_enabled)

        return ok

    # ------------------------------------------------------------------
    # Per-channel setpoints
    # ------------------------------------------------------------------

    def set_voltage(self, channel: int, voltage: float) -> bool:
        return self._driver.set_voltage(channel, voltage)

    def get_voltage_setpoint(self, channel: int) -> Optional[float]:
        return self._driver.get_voltage_setpoint(channel)

    def set_current(self, channel: int, current: float) -> bool:
        return self._driver.set_current(channel, current)

    def get_current_setpoint(self, channel: int) -> Optional[float]:
        return self._driver.get_current_setpoint(channel)

    def apply(self, channel: int, voltage: float, current: float) -> bool:
        """Set voltage and current limit in one command."""
        return self._driver.apply(channel, voltage, current)

    # ------------------------------------------------------------------
    # Output control
    # ------------------------------------------------------------------

    def output_on(self, channel: int) -> bool:
        return self._driver.output_on(channel)

    def output_off(self, channel: int) -> bool:
        return self._driver.output_off(channel)

    def is_output_on(self, channel: int) -> bool:
        return self._driver.get_output_state(channel) is True

    def select_channel(self, channel: int, selected: bool) -> bool:
        """Arm/disarm a channel for the master output switch."""
        return self._driver.set_channel_selected(channel, selected)

    def master_output_on(self) -> bool:
        """Enable all armed channels simultaneously via OUTP:GEN."""
        return self._driver.master_output_on()

    def master_output_off(self) -> bool:
        return self._driver.master_output_off()

    def all_outputs_on(self) -> bool:
        """Arm every channel and engage the master output."""
        for ch in range(1, self.num_channels + 1):
            self._driver.set_channel_selected(ch, True)
        return self._driver.master_output_on()

    def all_outputs_off(self) -> bool:
        """Disengage the master output switch (all channels off)."""
        return self._driver.master_output_off()

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def measure_voltage(self, channel: int) -> Optional[float]:
        return self._driver.measure_voltage(channel)

    def measure_current(self, channel: int) -> Optional[float]:
        return self._driver.measure_current(channel)

    def measure_power(self, channel: int) -> Optional[float]:
        return self._driver.measure_power(channel)

    def measure_all(self, channel: int) -> dict:
        """Return {'voltage': V, 'current': A, 'power': W} for one channel."""
        return {
            "voltage": self._driver.measure_voltage(channel),
            "current": self._driver.measure_current(channel),
            "power":   self._driver.measure_power(channel),
        }

    # ------------------------------------------------------------------
    # Voltage ramp (software, for callers that don't want EasyRamp on the box)
    # ------------------------------------------------------------------

    def ramp_voltage(self, channel: int, target_v: float,
                     step_v: float = 0.5, step_delay_s: float = 0.05) -> bool:
        """Ramp output voltage in software steps (alternative to EasyRamp)."""
        current_v = self._driver.get_voltage_setpoint(channel) or 0.0
        direction = 1 if target_v >= current_v else -1
        step      = abs(step_v) * direction

        v = current_v + step
        while direction * v < direction * target_v:
            self._driver.set_voltage(channel, max(VMIN, min(VMAX, v)))
            time.sleep(step_delay_s)
            v += step
        return self._driver.set_voltage(channel, target_v)

    # ------------------------------------------------------------------
    # Protection shortcuts
    # ------------------------------------------------------------------

    def set_ovp(self, channel: int, voltage: float, enabled: bool = True) -> bool:
        """Set OVP threshold and enable/disable."""
        self._driver.set_ovp_level(channel, voltage)
        return self._driver.set_ovp_state(channel, enabled)

    def clear_ovp(self, channel: int) -> bool:
        return self._driver.clear_ovp(channel)

    def is_ovp_tripped(self, channel: int) -> Optional[bool]:
        return self._driver.is_ovp_tripped(channel)

    def set_opp(self, channel: int, power: float, enabled: bool = True) -> bool:
        """Set OPP threshold and enable/disable."""
        self._driver.set_opp_level(channel, power)
        return self._driver.set_opp_state(channel, enabled)

    def clear_opp(self, channel: int) -> bool:
        return self._driver.clear_opp(channel)

    def is_opp_tripped(self, channel: int) -> Optional[bool]:
        return self._driver.is_opp_tripped(channel)

    def set_fuse(self, channel: int, enabled: bool,
                 delay_ms: Optional[float] = None) -> bool:
        """Enable/disable the electronic fuse, optionally setting trip delay."""
        if delay_ms is not None:
            self._driver.set_fuse_delay(channel, delay_ms)
        return self._driver.set_fuse_state(channel, enabled)

    def is_fuse_tripped(self, channel: int) -> Optional[bool]:
        return self._driver.is_fuse_tripped(channel)

    # ------------------------------------------------------------------
    # EasyRamp
    # ------------------------------------------------------------------

    def set_easyramp(self, channel: int, duration_ms: float,
                     enabled: bool = True) -> bool:
        """Configure the on-board EasyRamp soft-start."""
        self._driver.set_easyramp_duration(channel, duration_ms)
        return self._driver.set_easyramp_state(channel, enabled)

    # ------------------------------------------------------------------
    # Status snapshot
    # ------------------------------------------------------------------

    def get_status(self) -> InstrumentStatus:
        """Return a full InstrumentStatus snapshot."""
        st = InstrumentStatus(
            connected    = self.is_connected,
            idn          = self.idn,
            num_channels = self.num_channels,
        )
        if not self.is_connected:
            return st

        for ch in range(1, self.num_channels + 1):
            cs = ChannelStatus(channel=ch)
            cs.voltage_set      = self._driver.get_voltage_setpoint(ch) or 0.0
            cs.current_set      = self._driver.get_current_setpoint(ch) or 0.0
            cs.output_on        = self._driver.get_output_state(ch) is True
            cs.selected         = self._driver.get_channel_selected(ch) is True
            cs.measured_voltage = self._driver.measure_voltage(ch) or 0.0
            cs.measured_current = self._driver.measure_current(ch) or 0.0
            cs.measured_power   = self._driver.measure_power(ch)   or 0.0
            cs.ovp_enabled      = self._driver.get_ovp_state(ch) is True
            cs.ovp_level        = self._driver.get_ovp_level(ch) or 0.0
            cs.ovp_mode         = self._driver.get_ovp_mode(ch)  or ""
            cs.ovp_tripped      = self._driver.is_ovp_tripped(ch) is True
            cs.opp_enabled      = self._driver.get_opp_state(ch) is True
            cs.opp_level        = self._driver.get_opp_level(ch) or 0.0
            cs.opp_tripped      = self._driver.is_opp_tripped(ch) is True
            cs.fuse_enabled     = self._driver.get_fuse_state(ch) is True
            cs.fuse_delay_ms    = self._driver.get_fuse_delay(ch) or 0.0
            cs.fuse_tripped     = self._driver.is_fuse_tripped(ch) is True
            st.channels[ch] = cs
        return st

    def print_status(self) -> None:
        """Print a formatted instrument status summary."""
        st = self.get_status()
        print("\n" + "=" * 55)
        print("R&S NGE100 Status")
        print("=" * 55)
        print(f"Connected    : {st.connected}")
        if not st.connected:
            print("=" * 55)
            return

        print(f"IDN          : {st.idn}")
        print(f"Channels     : {st.num_channels}")
        for ch, cs in st.channels.items():
            print(f"\n--- Channel {ch} ---")
            print(f"  Output     : {'ON' if cs.output_on else 'OFF'}"
                  f"  (armed: {'YES' if cs.selected else 'no'})")
            print(f"  Setpoint   : {cs.voltage_set:.3f} V  /  {cs.current_set:.3f} A")
            print(f"  Measured   : {cs.measured_voltage:.3f} V  "
                  f"{cs.measured_current:.3f} A  {cs.measured_power:.3f} W")
            print(f"  OVP        : {'ON' if cs.ovp_enabled else 'OFF'} "
                  f"@ {cs.ovp_level:.2f} V  "
                  f"({'TRIPPED' if cs.ovp_tripped else 'ok'})")
            print(f"  OPP        : {'ON' if cs.opp_enabled else 'OFF'} "
                  f"@ {cs.opp_level:.2f} W  "
                  f"({'TRIPPED' if cs.opp_tripped else 'ok'})")
            print(f"  Fuse       : {'ON' if cs.fuse_enabled else 'OFF'} "
                  f"delay {cs.fuse_delay_ms:.0f} ms  "
                  f"({'TRIPPED' if cs.fuse_tripped else 'ok'})")
        print("=" * 55)

    # ------------------------------------------------------------------
    # Discovery passthrough
    # ------------------------------------------------------------------

    @staticmethod
    def discover() -> list[tuple[str, str]]:
        """Scan both VISA and serial transports for R&S NGE devices."""
        return NGE100Driver.discover()

    # ------------------------------------------------------------------
    # Simulation helpers
    # ------------------------------------------------------------------

    def sim_set_model(self, model: str) -> None:
        """Set simulated model string (call before connect())."""
        self._driver.sim_set_model(model)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
