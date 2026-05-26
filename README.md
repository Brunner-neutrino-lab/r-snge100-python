# nge100

Python driver and GUI for the Rohde & Schwarz NGE100 series DC power supply
(NGE102: 2 channels, NGE103: 3 channels).

Two-layer architecture (driver + controller), simulation mode, standalone GUI,
and a plugin interface (`MODULE_NAME` / `CONFIG_FIELDS` / `test()` / `read()`)
that matches the surrounding instrument-driver family.

## Two Transports, One API

The resource string picks the transport automatically:

| Resource string                               | Transport     |
|-----------------------------------------------|---------------|
| `USB0::0x0AAD::0x0197::<serial>::INSTR`       | VISA (USB TMC)|
| `TCPIP0::<ip>::INSTR`                         | VISA (LAN)    |
| `COM3` / `/dev/ttyUSB0`                       | pyserial (USB VCP) |

## Quick Start

```bash
pip install -r requirements.txt

python -m nge100.gui              # standalone GUI
python examples/basic_usage.py    # headless example
```

## API

```python
from nge100 import NGE100Controller

with NGE100Controller(resource="USB0::0x0AAD::0x0197::123::INSTR",
                      mode="hardware") as ps:

    # 1. Per-channel setpoint + output
    ps.apply(1, 5.0, 0.5)         # 5 V, 0.5 A limit
    ps.output_on(1)
    meas = ps.measure_all(1)      # {'voltage': .., 'current': .., 'power': ..}

    # 2. Coordinated bring-up via the master output switch
    ps.apply(2, 12.0, 1.0)
    ps.select_channel(1, True); ps.select_channel(2, True)
    ps.master_output_on()         # both channels live at once

    # 3. Protection
    ps.set_ovp(1, voltage=5.5)                       # threshold + enable
    ps.set_opp(1, power=5.0)
    ps.set_fuse(1, enabled=True, delay_ms=100)
    ps.set_easyramp(1, duration_ms=500)

    # 4. Software voltage ramp (alternative to EasyRamp)
    ps.ramp_voltage(1, target_v=10.0, step_v=0.5, step_delay_s=0.05)

    # 5. Status snapshot
    status = ps.get_status()       # InstrumentStatus dataclass
    ps.print_status()              # formatted to stdout
```

## Plugin Interface

`NGE100Controller` exposes the same class-level plugin attributes as the
other instrument modules in this family, so it auto-discovers in a host
application:

- `MODULE_NAME`, `DEVICE_NAME`
- `CONFIG_FIELDS` (list of {key, label, type, default, choices?})
- `DEFAULTS` (dict)
- `staticmethod test(config) -> (bool, str)`
- `staticmethod read(config) -> dict`

## Discovery

```python
from nge100 import NGE100Controller

for resource, idn in NGE100Controller.discover():
    print(resource, "→", idn)
```

Scans both VISA resources and COM ports (filtered to R&S USB VID 0x0AAD).

## Simulation Mode

`mode="simulation"` returns synthetic SCPI responses with realistic per-channel
state (setpoints, OVP/OPP/fuse, output state, master switch). Use
`sim_set_model("NGE102B")` / `sim_set_model("NGE103B")` to pick the channel
count before connecting.
