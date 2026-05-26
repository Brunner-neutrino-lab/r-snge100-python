"""
nge100/driver.py

Low-level SCPI interface to the Rohde & Schwarz NGE100 series DC power supply
(NGE102: 2 channels, NGE103: 3 channels).

Handles only instrument communication — no experiment logic, no Qt.

Two modes:
  "hardware"   — connects to the instrument; transport is auto-detected from the
                 resource string:
                    "COM3"                              → pyserial (USB VCP)
                    "USB0::0x0AAD::0x0197::...::INSTR"  → pyvisa (USB TMC)
                    "TCPIP0::192.168.1.10::INSTR"       → pyvisa (LAN)
  "simulation" — returns synthetic responses for development without hardware.

SCPI commands verified against the R&S NGE100 SCPI manual.
"""

import time
import threading
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_RESOURCE = "USB0::0x0AAD::0x0197::0::INSTR"
TIMEOUT_MS       = 3_000
IDN_EXPECTED     = "Rohde&Schwarz,NGE"

# Per-channel hardware limits
VMAX = 32.0   # V
VMIN = 0.0
IMAX = 3.0    # A
IMIN = 0.0
PMAX = 32.0   # W

# Serial defaults (USB VCP)
SERIAL_BAUDRATE = 9600
SERIAL_TIMEOUT  = 2.0

# R&S USB vendor ID (used by discovery)
RS_USB_VID = 0x0AAD
RS_MODELS  = ("NGE100", "NGE102", "NGE103")


def _is_com_port(resource: str) -> bool:
    """True if the resource string looks like a serial COM port."""
    r = (resource or "").strip().upper()
    return r.startswith("COM") or r.startswith("/DEV/TTY")


class NGE100Driver:
    """
    Low-level SCPI driver for the R&S NGE100 series.

    Parameters
    ----------
    resource : str
        VISA resource string (e.g. "USB0::0x0AAD::0x0197::12345::INSTR" or
        "TCPIP0::192.168.1.10::INSTR") OR a serial port name ("COM3").
        The transport is auto-selected from the string.
    mode : str
        "hardware" or "simulation".
    """

    def __init__(self, resource: str = DEFAULT_RESOURCE, mode: str = "simulation"):
        if mode not in ("hardware", "simulation"):
            raise ValueError(f"mode must be 'hardware' or 'simulation', got {mode!r}")
        self._resource   = resource
        self._mode       = mode
        self._inst       = None    # pyvisa Resource or pyserial Serial
        self._rm         = None    # pyvisa ResourceManager
        self._transport  = "serial" if _is_com_port(resource) else "visa"
        self._connected  = False
        self._idn        = None
        self._lock       = threading.Lock()

        # Number of channels — inferred from IDN after connect
        self._num_channels = 0

        # Simulation: per-channel state
        # ch -> dict(volt_set, curr_set, output_on, selected, ovp_level,
        #            ovp_enabled, ovp_mode, ovp_tripped, opp_level,
        #            opp_enabled, opp_tripped, fuse_enabled, fuse_delay_ms,
        #            fuse_tripped, ramp_enabled, ramp_duration_ms)
        self._sim_state: dict[int, dict] = {}
        self._sim_selected: int = 1
        self._sim_master_on: bool = False
        self._sim_model: str = "NGE103B"   # 3-channel by default in sim

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self):
        if self._connected:
            return
        if self._mode == "hardware":
            self._connect_hardware()
        else:
            self._connect_simulation()
        self._connected = True

    def disconnect(self):
        if not self._connected:
            return
        try:
            if self._mode == "hardware":
                self._disconnect_hardware()
        finally:
            self._connected = False
            self._inst = None
            self._idn  = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def resource(self) -> str:
        return self._resource

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def idn(self) -> Optional[str]:
        return self._idn

    @property
    def num_channels(self) -> int:
        return self._num_channels

    # ------------------------------------------------------------------
    # Low-level SCPI
    # ------------------------------------------------------------------

    def write(self, command: str, sync: bool = False) -> bool:
        """Send a SCPI command (no response expected)."""
        with self._lock:
            if not self._connected:
                return False
            if self._mode == "hardware":
                return self._write_hardware(command, sync)
            return self._write_simulation(command, sync)

    def query(self, command: str, retries: int = 1) -> Optional[str]:
        """Send a SCPI query and return the response string (or None on error)."""
        with self._lock:
            if not self._connected:
                return None
            if self._mode == "hardware":
                return self._query_hardware(command, retries)
            return self._query_simulation(command)

    # ------------------------------------------------------------------
    # Channel select (internal helper for SCPI; instrument is select-then-command)
    # ------------------------------------------------------------------

    def select(self, channel: int) -> bool:
        """Select the active channel for subsequent commands (INST:NSEL)."""
        if channel < 1 or channel > max(self._num_channels, 3):
            raise ValueError(f"channel must be 1..{self._num_channels}, got {channel}")
        return self.write(f"INST:NSEL {channel}")

    # ------------------------------------------------------------------
    # Voltage / current setpoints
    # ------------------------------------------------------------------

    def set_voltage(self, channel: int, voltage: float) -> bool:
        if not (VMIN <= voltage <= VMAX):
            raise ValueError(f"voltage out of range [{VMIN}, {VMAX}] V: {voltage}")
        if not self.select(channel):
            return False
        return self.write(f"VOLT {voltage:.4f}")

    def get_voltage_setpoint(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("VOLT?"))

    def set_current(self, channel: int, current: float) -> bool:
        if not (IMIN <= current <= IMAX):
            raise ValueError(f"current out of range [{IMIN}, {IMAX}] A: {current}")
        if not self.select(channel):
            return False
        return self.write(f"CURR {current:.4f}")

    def get_current_setpoint(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("CURR?"))

    def apply(self, channel: int, voltage: float, current: float) -> bool:
        """Set voltage and current in one command (APPL)."""
        if not (VMIN <= voltage <= VMAX):
            raise ValueError(f"voltage out of range [{VMIN}, {VMAX}] V: {voltage}")
        if not (IMIN <= current <= IMAX):
            raise ValueError(f"current out of range [{IMIN}, {IMAX}] A: {current}")
        if not self.select(channel):
            return False
        return self.write(f'APPL "{voltage:.4f},{current:.4f}"')

    # ------------------------------------------------------------------
    # Output state
    # ------------------------------------------------------------------

    def output_on(self, channel: int) -> bool:
        if not self.select(channel):
            return False
        return self.write("OUTP ON")

    def output_off(self, channel: int) -> bool:
        if not self.select(channel):
            return False
        return self.write("OUTP OFF")

    def get_output_state(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("OUTP?"))

    def set_channel_selected(self, channel: int, selected: bool) -> bool:
        """Arm or disarm a channel for the master output switch (OUTP:SEL)."""
        if not self.select(channel):
            return False
        return self.write(f"OUTP:SEL {'ON' if selected else 'OFF'}")

    def get_channel_selected(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("OUTP:SEL?"))

    def master_output_on(self) -> bool:
        return self.write("OUTP:GEN ON")

    def master_output_off(self) -> bool:
        return self.write("OUTP:GEN OFF")

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def measure_voltage(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("MEAS:VOLT?"))

    def measure_current(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("MEAS:CURR?"))

    def measure_power(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("MEAS:POW?"))

    # ------------------------------------------------------------------
    # OVP / OPP / Fuse
    # ------------------------------------------------------------------

    def set_ovp_state(self, channel: int, enabled: bool) -> bool:
        if not self.select(channel):
            return False
        return self.write(f"VOLT:PROT {'ON' if enabled else 'OFF'}")

    def get_ovp_state(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("VOLT:PROT?"))

    def set_ovp_level(self, channel: int, voltage: float) -> bool:
        if not (VMIN <= voltage <= VMAX):
            raise ValueError(f"OVP level out of range [{VMIN}, {VMAX}] V: {voltage}")
        if not self.select(channel):
            return False
        return self.write(f"VOLT:PROT:LEV {voltage:.3f}")

    def get_ovp_level(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("VOLT:PROT:LEV?"))

    def set_ovp_mode(self, channel: int, mode: str) -> bool:
        """OVP mode: 'MEAS' (trip on measured) or 'PROT' (also block turn-on)."""
        m = mode.upper()
        if m not in ("MEAS", "PROT"):
            raise ValueError(f"ovp mode must be 'MEAS' or 'PROT', got {mode!r}")
        if not self.select(channel):
            return False
        return self.write(f"VOLT:PROT:MODE {m}")

    def get_ovp_mode(self, channel: int) -> Optional[str]:
        if not self.select(channel):
            return None
        return self.query("VOLT:PROT:MODE?")

    def is_ovp_tripped(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("VOLT:PROT:TRIP?"))

    def clear_ovp(self, channel: int) -> bool:
        if not self.select(channel):
            return False
        return self.write("VOLT:PROT:CLE")

    def set_opp_state(self, channel: int, enabled: bool) -> bool:
        if not self.select(channel):
            return False
        return self.write(f"POW:PROT {'ON' if enabled else 'OFF'}")

    def get_opp_state(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("POW:PROT?"))

    def set_opp_level(self, channel: int, power: float) -> bool:
        if not (0 <= power <= PMAX):
            raise ValueError(f"OPP level out of range [0, {PMAX}] W: {power}")
        if not self.select(channel):
            return False
        return self.write(f"POW:PROT:LEV {power:.3f}")

    def get_opp_level(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("POW:PROT:LEV?"))

    def is_opp_tripped(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("POW:PROT:TRIP?"))

    def clear_opp(self, channel: int) -> bool:
        if not self.select(channel):
            return False
        return self.write("POW:PROT:CLE")

    def set_fuse_state(self, channel: int, enabled: bool) -> bool:
        if not self.select(channel):
            return False
        return self.write(f"FUSE {'ON' if enabled else 'OFF'}")

    def get_fuse_state(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("FUSE?"))

    def set_fuse_delay(self, channel: int, delay_ms: float) -> bool:
        if not (0 <= delay_ms <= 10_000):
            raise ValueError(f"fuse delay out of range [0, 10000] ms: {delay_ms}")
        if not self.select(channel):
            return False
        return self.write(f"FUSE:DEL {int(delay_ms)}")

    def get_fuse_delay(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("FUSE:DEL?"))

    def link_fuse(self, channel: int, linked_channel: int) -> bool:
        if not self.select(channel):
            return False
        return self.write(f"FUSE:LINK {linked_channel}")

    def unlink_fuse(self, channel: int, linked_channel: int) -> bool:
        if not self.select(channel):
            return False
        return self.write(f"FUSE:UNL {linked_channel}")

    def is_fuse_tripped(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("FUSE:TRIP?"))

    # ------------------------------------------------------------------
    # EasyRamp
    # ------------------------------------------------------------------

    def set_easyramp_state(self, channel: int, enabled: bool) -> bool:
        if not self.select(channel):
            return False
        return self.write(f"VOLT:RAMP {'ON' if enabled else 'OFF'}")

    def get_easyramp_state(self, channel: int) -> Optional[bool]:
        if not self.select(channel):
            return None
        return _to_bool(self.query("VOLT:RAMP?"))

    def set_easyramp_duration(self, channel: int, duration_ms: float) -> bool:
        if not (10 <= duration_ms <= 10_000):
            raise ValueError(f"ramp duration out of range [10, 10000] ms: {duration_ms}")
        if not self.select(channel):
            return False
        return self.write(f"VOLT:RAMP:DUR {int(duration_ms)}")

    def get_easyramp_duration(self, channel: int) -> Optional[float]:
        if not self.select(channel):
            return None
        return _to_float(self.query("VOLT:RAMP:DUR?"))

    # ------------------------------------------------------------------
    # IEEE 488.2 common commands
    # ------------------------------------------------------------------

    def reset(self) -> bool:
        return self.write("*RST")

    def clear_status(self) -> bool:
        return self.write("*CLS")

    def get_error(self) -> Optional[str]:
        return self.query("SYST:ERR?")

    def set_local(self) -> bool:
        return self.write("SYST:LOC")

    def set_remote(self) -> bool:
        return self.write("SYST:REM")

    def beep(self) -> bool:
        return self.write("SYST:BEEP")

    # ------------------------------------------------------------------
    # Hardware internals
    # ------------------------------------------------------------------

    def _connect_hardware(self):
        if self._transport == "serial":
            self._connect_serial()
        else:
            self._connect_visa()

        # Verify IDN
        self._idn = self._query_hardware("*IDN?", retries=1)
        if not self._idn or IDN_EXPECTED.lower() not in self._idn.lower():
            self._disconnect_hardware()
            raise RuntimeError(
                f"IDN mismatch. Expected substring {IDN_EXPECTED!r}, got {self._idn!r}\n"
                f"Check resource string: {self._resource!r}"
            )
        self._num_channels = self._detect_num_channels(self._idn)

    def _connect_visa(self):
        try:
            import pyvisa
        except ImportError as e:
            raise ImportError(
                "pyvisa not installed. Run: pip install pyvisa pyvisa-py"
            ) from e

        self._rm = pyvisa.ResourceManager()
        attempts = 0
        last_err = None
        while attempts < 3:
            try:
                self._inst = self._rm.open_resource(self._resource)
                break
            except Exception as e:
                last_err = e
                attempts += 1
                time.sleep(0.5)
        else:
            raise RuntimeError(
                f"Could not open VISA resource {self._resource!r} after 3 attempts: {last_err}"
            )

        self._inst.timeout           = TIMEOUT_MS
        self._inst.read_termination  = "\n"
        self._inst.write_termination = "\n"

    def _connect_serial(self):
        try:
            import serial as _serial
        except ImportError as e:
            raise ImportError(
                "pyserial not installed. Run: pip install pyserial"
            ) from e

        try:
            self._inst = _serial.Serial(
                port     = self._resource,
                baudrate = SERIAL_BAUDRATE,
                bytesize = _serial.EIGHTBITS,
                parity   = _serial.PARITY_NONE,
                stopbits = _serial.STOPBITS_ONE,
                timeout  = SERIAL_TIMEOUT,
            )
        except Exception as e:
            raise RuntimeError(
                f"Could not open serial port {self._resource!r}: {e}"
            ) from e
        time.sleep(0.2)
        self._inst.reset_input_buffer()

    def _disconnect_hardware(self):
        if self._inst is not None:
            try:
                # Return to local control before closing
                if self._transport == "visa":
                    try:
                        self._inst.write("SYST:LOC")
                    except Exception:
                        pass
                else:
                    try:
                        self._inst.write(b"SYST:LOC\n")
                    except Exception:
                        pass
                self._inst.close()
            except Exception:
                pass
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None

    def _write_hardware(self, command: str, sync: bool) -> bool:
        try:
            if self._transport == "visa":
                self._inst.write(command)
                if sync:
                    self._inst.query("*OPC?")
            else:
                self._inst.write((command + "\n").encode("ascii"))
                time.sleep(0.05)
                if sync:
                    self._sync_serial_locked()
            return True
        except Exception:
            return False

    def _query_hardware(self, command: str, retries: int) -> Optional[str]:
        for attempt in range(1 + retries):
            try:
                if self._transport == "visa":
                    resp = self._inst.query(command)
                    return resp.strip() if resp else None
                else:
                    self._inst.reset_input_buffer()
                    self._inst.write((command + "\n").encode("ascii"))
                    time.sleep(0.15)
                    resp = self._inst.readline().decode("ascii", errors="ignore").strip()
                    if resp:
                        return resp
            except Exception:
                if attempt >= retries:
                    return None
        return None

    def _sync_serial_locked(self) -> None:
        try:
            self._inst.reset_input_buffer()
            self._inst.write(b"*OPC?\n")
            time.sleep(0.1)
            self._inst.readline()
        except Exception:
            pass

    @staticmethod
    def _detect_num_channels(idn: str) -> int:
        up = (idn or "").upper()
        if "NGE103" in up:
            return 3
        if "NGE102" in up:
            return 2
        return 2

    # ------------------------------------------------------------------
    # Discovery (static helpers)
    # ------------------------------------------------------------------

    @staticmethod
    def discover_serial() -> list[tuple[str, str]]:
        """Scan COM ports for R&S NGE instruments. Returns [(port, idn), ...]."""
        try:
            import serial as _serial
            import serial.tools.list_ports as _lp
        except ImportError:
            return []
        found = []
        for p in _lp.comports():
            hwid = (p.hwid or "").upper()
            if "BTHENUM" in hwid:
                continue
            if p.vid is not None and p.vid != RS_USB_VID:
                continue
            try:
                ser = _serial.Serial(p.device, SERIAL_BAUDRATE,
                                     timeout=0.5, write_timeout=0.5)
                time.sleep(0.1)
                ser.reset_input_buffer()
                ser.write(b"*IDN?\n")
                time.sleep(0.3)
                resp = ser.readline().decode("ascii", errors="ignore").strip()
                ser.close()
                if resp and any(m in resp.upper() for m in RS_MODELS):
                    found.append((p.device, resp))
            except Exception:
                pass
        return found

    @staticmethod
    def discover_visa() -> list[tuple[str, str]]:
        """Scan VISA resources for R&S NGE instruments. Returns [(resource, idn), ...]."""
        try:
            import pyvisa
        except ImportError:
            return []
        found = []
        try:
            rm = pyvisa.ResourceManager()
            for res in rm.list_resources():
                try:
                    inst = rm.open_resource(res)
                    inst.timeout = 500
                    inst.read_termination  = "\n"
                    inst.write_termination = "\n"
                    idn = inst.query("*IDN?").strip()
                    inst.close()
                    if idn and any(m in idn.upper() for m in RS_MODELS):
                        found.append((res, idn))
                except Exception:
                    pass
            rm.close()
        except Exception:
            pass
        return found

    @staticmethod
    def discover() -> list[tuple[str, str]]:
        """Scan both VISA and serial transports."""
        return NGE100Driver.discover_visa() + NGE100Driver.discover_serial()

    # ------------------------------------------------------------------
    # Simulation internals
    # ------------------------------------------------------------------

    def _connect_simulation(self):
        self._idn = f"Rohde&Schwarz,{self._sim_model},900000/000,1.50"
        self._num_channels = self._detect_num_channels(self._sim_model)
        for ch in range(1, self._num_channels + 1):
            self._sim_state[ch] = {
                "volt_set":         0.0,
                "curr_set":         0.1,
                "output_on":        False,
                "selected":         False,
                "ovp_level":        32.0,
                "ovp_enabled":      True,
                "ovp_mode":         "MEAS",
                "ovp_tripped":      False,
                "opp_level":        32.0,
                "opp_enabled":      False,
                "opp_tripped":      False,
                "fuse_enabled":     False,
                "fuse_delay_ms":    0,
                "fuse_tripped":     False,
                "ramp_enabled":     False,
                "ramp_duration_ms": 100,
            }
        self._sim_selected  = 1
        self._sim_master_on = False

    def _write_simulation(self, command: str, sync: bool) -> bool:
        try:
            self._handle_sim_command(command)
            return True
        except Exception:
            return False

    def _query_simulation(self, command: str) -> Optional[str]:
        try:
            return self._handle_sim_query(command)
        except Exception:
            return None

    def _sim_ch(self) -> dict:
        return self._sim_state.setdefault(self._sim_selected, self._sim_state[1])

    def _handle_sim_command(self, command: str) -> None:
        c = command.strip()
        up = c.upper()

        if up.startswith("INST:NSEL"):
            self._sim_selected = int(c.split()[-1])
            return
        if up == "*RST":
            self._connect_simulation()
            return
        if up == "*CLS":
            for st in self._sim_state.values():
                st["ovp_tripped"]  = False
                st["opp_tripped"]  = False
                st["fuse_tripped"] = False
            return
        if up in ("SYST:LOC", "SYST:REM", "SYST:BEEP"):
            return

        st = self._sim_ch()

        if up.startswith("VOLT:PROT:CLE"):
            st["ovp_tripped"] = False; return
        if up.startswith("POW:PROT:CLE"):
            st["opp_tripped"] = False; return
        if up.startswith("VOLT:PROT:LEV"):
            st["ovp_level"] = float(c.split()[-1]); return
        if up.startswith("VOLT:PROT:MODE"):
            st["ovp_mode"] = c.split()[-1].upper(); return
        if up.startswith("VOLT:PROT"):
            st["ovp_enabled"] = _on(c); return
        if up.startswith("POW:PROT:LEV"):
            st["opp_level"] = float(c.split()[-1]); return
        if up.startswith("POW:PROT"):
            st["opp_enabled"] = _on(c); return
        if up.startswith("FUSE:DEL"):
            st["fuse_delay_ms"] = float(c.split()[-1]); return
        if up.startswith("FUSE:LINK") or up.startswith("FUSE:UNL"):
            return
        if up.startswith("FUSE"):
            st["fuse_enabled"] = _on(c); return
        if up.startswith("VOLT:RAMP:DUR"):
            st["ramp_duration_ms"] = float(c.split()[-1]); return
        if up.startswith("VOLT:RAMP"):
            st["ramp_enabled"] = _on(c); return
        if up.startswith("VOLT"):
            st["volt_set"] = float(c.split()[-1]); return
        if up.startswith("CURR"):
            st["curr_set"] = float(c.split()[-1]); return
        if up.startswith("APPL"):
            # APPL "V,I"
            payload = c.split(None, 1)[1].strip().strip('"').strip("'")
            v_str, i_str = payload.split(",")
            st["volt_set"] = float(v_str)
            st["curr_set"] = float(i_str)
            return
        if up.startswith("OUTP:GEN"):
            self._sim_master_on = _on(c)
            # Armed channels follow the master switch
            for s in self._sim_state.values():
                if s["selected"]:
                    s["output_on"] = self._sim_master_on
            return
        if up.startswith("OUTP:SEL"):
            st["selected"] = _on(c); return
        if up.startswith("OUTP"):
            st["output_on"] = _on(c); return

    def _handle_sim_query(self, command: str) -> Optional[str]:
        up = command.strip().upper().rstrip("?")
        st = self._sim_ch()

        if up == "*IDN":
            return self._idn
        if up == "*OPC":
            return "1"
        if up == "SYST:ERR":
            return '0,"No error"'

        if up == "VOLT":               return f"{st['volt_set']:.4f}"
        if up == "CURR":               return f"{st['curr_set']:.4f}"
        if up == "MEAS:VOLT":
            return f"{(st['volt_set'] if st['output_on'] else 0.0):.4f}"
        if up == "MEAS:CURR":
            # crude: half the limit if output on, else ~0
            return f"{(min(st['curr_set'] * 0.5, st['curr_set']) if st['output_on'] else 0.0):.4f}"
        if up == "MEAS:POW":
            v = st['volt_set'] if st['output_on'] else 0.0
            i = st['curr_set'] * 0.5 if st['output_on'] else 0.0
            return f"{v * i:.4f}"
        if up == "OUTP":               return "1" if st["output_on"] else "0"
        if up == "OUTP:SEL":           return "1" if st["selected"]  else "0"
        if up == "OUTP:GEN":           return "1" if self._sim_master_on else "0"
        if up == "VOLT:PROT":          return "1" if st["ovp_enabled"] else "0"
        if up == "VOLT:PROT:LEV":      return f"{st['ovp_level']:.3f}"
        if up == "VOLT:PROT:MODE":     return st["ovp_mode"]
        if up == "VOLT:PROT:TRIP":     return "1" if st["ovp_tripped"] else "0"
        if up == "POW:PROT":           return "1" if st["opp_enabled"] else "0"
        if up == "POW:PROT:LEV":       return f"{st['opp_level']:.3f}"
        if up == "POW:PROT:TRIP":      return "1" if st["opp_tripped"] else "0"
        if up == "FUSE":               return "1" if st["fuse_enabled"] else "0"
        if up == "FUSE:DEL":           return f"{st['fuse_delay_ms']:.0f}"
        if up == "FUSE:TRIP":          return "1" if st["fuse_tripped"] else "0"
        if up == "VOLT:RAMP":          return "1" if st["ramp_enabled"] else "0"
        if up == "VOLT:RAMP:DUR":      return f"{st['ramp_duration_ms']:.0f}"
        if up == "APPL":
            return f'"{st["volt_set"]:.4f},{st["curr_set"]:.4f}"'
        return None

    def sim_set_model(self, model: str) -> None:
        """Set simulated model string ('NGE102B' or 'NGE103B'). Call before connect()."""
        m = model.upper()
        if "NGE102" not in m and "NGE103" not in m:
            raise ValueError(f"model must contain 'NGE102' or 'NGE103', got {model!r}")
        self._sim_model = model

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(resp: Optional[str]) -> Optional[float]:
    if resp is None:
        return None
    try:
        return float(resp)
    except (ValueError, TypeError):
        return None


def _to_bool(resp: Optional[str]) -> Optional[bool]:
    if resp is None:
        return None
    return resp.strip().upper() in ("1", "ON")


def _on(command: str) -> bool:
    """Parse ON/OFF/1/0 from the tail of a SCPI command."""
    tail = command.strip().split()[-1].upper()
    return tail in ("1", "ON")
