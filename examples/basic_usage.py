"""
NGE100 basic usage example — simulation mode.

Demonstrates the three main operating patterns:
  1. Per-channel setpoint + output enable
  2. Coordinated bring-up of multiple channels via master output
  3. Protection (OVP / OPP / Fuse) configuration
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nge100 import NGE100Controller

ctrl = NGE100Controller(mode="simulation")
ctrl.sim_set_model("NGE103B")   # 3-channel sim

with ctrl:
    ctrl.configure(default_voltage=5.0, default_current=0.5, default_ovp=10.0)

    # --- 1. Per-channel apply + output ---
    print("1. Per-channel apply")
    ctrl.apply(1, 5.0, 0.5)
    ctrl.output_on(1)
    meas = ctrl.measure_all(1)
    print(f"   CH1: set 5.0 V / 0.5 A   ->   measured "
          f"{meas['voltage']:.3f} V  {meas['current']:.3f} A  {meas['power']:.3f} W")

    # --- 2. Coordinated bring-up via master output ---
    print("\n2. Master output (coordinated bring-up)")
    ctrl.apply(2, 12.0, 1.0)
    ctrl.apply(3, 3.3,  0.2)
    for ch in (1, 2, 3):
        ctrl.select_channel(ch, True)        # arm
        ctrl.output_off(ch)                  # individually off
    ctrl.master_output_on()                  # bring up all armed channels
    for ch in (1, 2, 3):
        m = ctrl.measure_all(ch)
        print(f"   CH{ch}: {m['voltage']:.3f} V  {m['current']:.3f} A  {m['power']:.3f} W")
    ctrl.master_output_off()

    # --- 3. Protection ---
    print("\n3. Protection")
    ctrl.set_ovp(1, voltage=5.5)             # OVP @ 5.5 V, enabled
    ctrl.set_opp(1, power=5.0)               # OPP @ 5.0 W, enabled
    ctrl.set_fuse(1, enabled=True, delay_ms=100)
    ctrl.set_easyramp(1, duration_ms=500)
    print("   CH1: OVP=5.5 V, OPP=5.0 W, Fuse=ON (100 ms), EasyRamp=500 ms")

    # --- Full snapshot ---
    print("\n4. Status snapshot")
    ctrl.print_status()

print("\nDone.")
