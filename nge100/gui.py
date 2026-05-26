"""
nge100/gui.py

Standalone PyQt5 GUI for the Rohde & Schwarz NGE100 power supply.

Launch directly:
    python -m nge100.gui

Tabs:
    Connection  — Resource string (VISA or COM port), mode, connect/disconnect
    Channels    — Per-channel V/I setpoints, output state, live measurements
    Protection  — OVP / OPP / Fuse / EasyRamp per channel
    Status      — Full instrument snapshot
"""

import sys
import time

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QComboBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QCheckBox, QTextEdit, QTabWidget, QGridLayout,
)
from PyQt5.QtCore import QThread, pyqtSignal, QObject, QTimer
from PyQt5.QtGui import QFont

from .controller import NGE100Controller, InstrumentStatus
from .driver import DEFAULT_RESOURCE


# ---------------------------------------------------------------------------
# Worker signals & threads
# ---------------------------------------------------------------------------

class _Signals(QObject):
    status         = pyqtSignal(str)
    connected      = pyqtSignal(bool, str)
    measure_done   = pyqtSignal(int, object)              # channel, dict
    status_done    = pyqtSignal(object)                   # InstrumentStatus
    output_changed = pyqtSignal(int, bool)                # channel, on


class _ConnectWorker(QThread):
    def __init__(self, ctrl, signals):
        super().__init__()
        self._ctrl = ctrl; self._signals = signals

    def run(self):
        try:
            self._ctrl.connect()
            self._signals.connected.emit(True, self._ctrl.identify())
        except Exception as e:
            self._signals.connected.emit(False, str(e))


class _MeasureWorker(QThread):
    def __init__(self, ctrl, channel, signals):
        super().__init__()
        self._ctrl = ctrl; self._ch = channel; self._signals = signals

    def run(self):
        try:
            meas = self._ctrl.measure_all(self._ch)
            self._signals.measure_done.emit(self._ch, meas)
        except Exception as e:
            self._signals.status.emit(f"Measure error (CH{self._ch}): {e}")


class _StatusWorker(QThread):
    def __init__(self, ctrl, signals):
        super().__init__()
        self._ctrl = ctrl; self._signals = signals

    def run(self):
        try:
            st = self._ctrl.get_status()
            self._signals.status_done.emit(st)
        except Exception as e:
            self._signals.status.emit(f"Status error: {e}")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class NGE100Window(QMainWindow):

    MAX_CHANNELS = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("R&S NGE100 Power Supply Control")
        self.resize(880, 720)

        self._ctrl: NGE100Controller | None = None
        self._signals = _Signals()
        self._worker  = None
        self._num_channels = self.MAX_CHANNELS  # widget count; trimmed after connect

        # Per-channel widgets (1-indexed lists, [0] is unused)
        self._ch_volt:   list[QDoubleSpinBox | None] = [None] * (self.MAX_CHANNELS + 1)
        self._ch_curr:   list[QDoubleSpinBox | None] = [None] * (self.MAX_CHANNELS + 1)
        self._ch_on_btn: list[QPushButton | None]    = [None] * (self.MAX_CHANNELS + 1)
        self._ch_off_btn:list[QPushButton | None]    = [None] * (self.MAX_CHANNELS + 1)
        self._ch_apply:  list[QPushButton | None]    = [None] * (self.MAX_CHANNELS + 1)
        self._ch_arm:    list[QCheckBox | None]      = [None] * (self.MAX_CHANNELS + 1)
        self._ch_meas:   list[QLabel | None]         = [None] * (self.MAX_CHANNELS + 1)
        self._ch_state:  list[QLabel | None]         = [None] * (self.MAX_CHANNELS + 1)

        self._prot_ovp_lvl:  list[QDoubleSpinBox | None] = [None] * (self.MAX_CHANNELS + 1)
        self._prot_ovp_en:   list[QCheckBox | None]      = [None] * (self.MAX_CHANNELS + 1)
        self._prot_opp_lvl:  list[QDoubleSpinBox | None] = [None] * (self.MAX_CHANNELS + 1)
        self._prot_opp_en:   list[QCheckBox | None]      = [None] * (self.MAX_CHANNELS + 1)
        self._prot_fuse_en:  list[QCheckBox | None]      = [None] * (self.MAX_CHANNELS + 1)
        self._prot_fuse_del: list[QDoubleSpinBox | None] = [None] * (self.MAX_CHANNELS + 1)
        self._prot_ramp_en:  list[QCheckBox | None]      = [None] * (self.MAX_CHANNELS + 1)
        self._prot_ramp_dur: list[QDoubleSpinBox | None] = [None] * (self.MAX_CHANNELS + 1)
        self._prot_apply:    list[QPushButton | None]    = [None] * (self.MAX_CHANNELS + 1)

        self._build_ui()
        self._connect_signals()

        # Periodic measurement refresh (only when connected and on Channels tab)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(750)
        self._poll_timer.timeout.connect(self._poll_measurements)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_connection_tab(), "Connection")
        self._tabs.addTab(self._build_channels_tab(),   "Channels")
        self._tabs.addTab(self._build_protection_tab(), "Protection")
        self._tabs.addTab(self._build_status_tab(),     "Status")

        lay.addWidget(self._tabs)
        lay.addWidget(self._build_log())

        self._tabs.currentChanged.connect(self._on_tab_changed)

    # --- Connection ---
    def _build_connection_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("Instrument Connection")
        g   = QGridLayout(box)

        g.addWidget(QLabel("Resource (VISA or COM port):"), 0, 0)
        self._resource_edit = QLineEdit(DEFAULT_RESOURCE)
        g.addWidget(self._resource_edit, 0, 1)

        g.addWidget(QLabel("Mode:"), 1, 0)
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["simulation", "hardware"])
        g.addWidget(self._mode_combo, 1, 1)

        btn_row = QHBoxLayout()
        self._connect_btn    = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._test_btn       = QPushButton("Test")
        self._discover_btn   = QPushButton("Discover")
        self._disconnect_btn.setEnabled(False)
        btn_row.addWidget(self._connect_btn)
        btn_row.addWidget(self._disconnect_btn)
        btn_row.addWidget(self._test_btn)
        btn_row.addWidget(self._discover_btn)
        g.addLayout(btn_row, 2, 0, 1, 2)

        self._conn_label = QLabel("Not connected")
        self._conn_label.setStyleSheet("color: red; font-weight: bold;")
        g.addWidget(self._conn_label, 3, 0, 1, 2)

        # Master output switch
        master_box = QGroupBox("Master Output (OUTP:GEN)")
        mg = QHBoxLayout(master_box)
        self._master_on_btn  = QPushButton("Master ON")
        self._master_off_btn = QPushButton("Master OFF")
        self._master_on_btn.setEnabled(False)
        self._master_off_btn.setEnabled(False)
        mg.addWidget(self._master_on_btn)
        mg.addWidget(self._master_off_btn)

        lay.addWidget(box)
        lay.addWidget(master_box)
        lay.addStretch()
        return w

    # --- Channels ---
    def _build_channels_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)

        for ch in range(1, self.MAX_CHANNELS + 1):
            box = QGroupBox(f"Channel {ch}")
            g   = QGridLayout(box)

            g.addWidget(QLabel("Voltage (V):"), 0, 0)
            v = QDoubleSpinBox()
            v.setRange(0.0, 32.0); v.setDecimals(3); v.setSingleStep(0.1); v.setValue(0.0)
            self._ch_volt[ch] = v
            g.addWidget(v, 0, 1)

            g.addWidget(QLabel("Current limit (A):"), 0, 2)
            i = QDoubleSpinBox()
            i.setRange(0.0, 3.0); i.setDecimals(3); i.setSingleStep(0.01); i.setValue(0.1)
            self._ch_curr[ch] = i
            g.addWidget(i, 0, 3)

            apply_btn = QPushButton("Apply")
            apply_btn.setEnabled(False)
            self._ch_apply[ch] = apply_btn
            g.addWidget(apply_btn, 0, 4)

            on_btn  = QPushButton("Output ON")
            off_btn = QPushButton("Output OFF")
            on_btn.setEnabled(False); off_btn.setEnabled(False)
            self._ch_on_btn[ch]  = on_btn
            self._ch_off_btn[ch] = off_btn
            g.addWidget(on_btn, 1, 1)
            g.addWidget(off_btn, 1, 2)

            arm = QCheckBox("Arm for master")
            arm.setEnabled(False)
            self._ch_arm[ch] = arm
            g.addWidget(arm, 1, 3, 1, 2)

            state_lbl = QLabel("Output: OFF")
            state_lbl.setStyleSheet("color: red;")
            self._ch_state[ch] = state_lbl
            g.addWidget(state_lbl, 2, 1, 1, 2)

            meas_lbl = QLabel("— V    — A    — W")
            meas_lbl.setFont(QFont("Courier", 11))
            self._ch_meas[ch] = meas_lbl
            g.addWidget(meas_lbl, 2, 3, 1, 2)

            lay.addWidget(box)
        lay.addStretch()
        return w

    # --- Protection ---
    def _build_protection_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        for ch in range(1, self.MAX_CHANNELS + 1):
            box = QGroupBox(f"Channel {ch} — Protection")
            g   = QGridLayout(box)

            # OVP
            g.addWidget(QLabel("OVP level (V):"), 0, 0)
            ovp_lvl = QDoubleSpinBox()
            ovp_lvl.setRange(0.0, 32.0); ovp_lvl.setDecimals(2); ovp_lvl.setValue(32.0)
            self._prot_ovp_lvl[ch] = ovp_lvl
            g.addWidget(ovp_lvl, 0, 1)
            ovp_en = QCheckBox("Enabled")
            self._prot_ovp_en[ch] = ovp_en
            g.addWidget(ovp_en, 0, 2)

            # OPP
            g.addWidget(QLabel("OPP level (W):"), 1, 0)
            opp_lvl = QDoubleSpinBox()
            opp_lvl.setRange(0.0, 32.0); opp_lvl.setDecimals(2); opp_lvl.setValue(32.0)
            self._prot_opp_lvl[ch] = opp_lvl
            g.addWidget(opp_lvl, 1, 1)
            opp_en = QCheckBox("Enabled")
            self._prot_opp_en[ch] = opp_en
            g.addWidget(opp_en, 1, 2)

            # Fuse
            g.addWidget(QLabel("Fuse delay (ms):"), 2, 0)
            fdel = QDoubleSpinBox()
            fdel.setRange(0, 10_000); fdel.setDecimals(0); fdel.setValue(0)
            self._prot_fuse_del[ch] = fdel
            g.addWidget(fdel, 2, 1)
            fuse_en = QCheckBox("Enabled")
            self._prot_fuse_en[ch] = fuse_en
            g.addWidget(fuse_en, 2, 2)

            # EasyRamp
            g.addWidget(QLabel("EasyRamp duration (ms):"), 3, 0)
            ramp_dur = QDoubleSpinBox()
            ramp_dur.setRange(10, 10_000); ramp_dur.setDecimals(0); ramp_dur.setValue(100)
            self._prot_ramp_dur[ch] = ramp_dur
            g.addWidget(ramp_dur, 3, 1)
            ramp_en = QCheckBox("Enabled")
            self._prot_ramp_en[ch] = ramp_en
            g.addWidget(ramp_en, 3, 2)

            apply_btn = QPushButton("Apply Protection")
            apply_btn.setEnabled(False)
            self._prot_apply[ch] = apply_btn
            g.addWidget(apply_btn, 4, 0, 1, 3)

            lay.addWidget(box)
        lay.addStretch()
        return w

    # --- Status ---
    def _build_status_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        self._refresh_btn = QPushButton("Refresh Status")
        self._refresh_btn.setEnabled(False)
        lay.addWidget(self._refresh_btn)
        self._status_text = QTextEdit()
        self._status_text.setReadOnly(True)
        self._status_text.setFont(QFont("Courier", 9))
        lay.addWidget(self._status_text)
        return w

    def _build_log(self) -> QWidget:
        box = QGroupBox("Status Log")
        lay = QVBoxLayout(box)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(110)
        self._log.setFont(QFont("Courier", 9))
        lay.addWidget(self._log)
        return box

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self._connect_btn.clicked.connect(self._on_connect)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        self._test_btn.clicked.connect(self._on_test)
        self._discover_btn.clicked.connect(self._on_discover)
        self._master_on_btn.clicked.connect(self._on_master_on)
        self._master_off_btn.clicked.connect(self._on_master_off)
        self._refresh_btn.clicked.connect(self._on_refresh_status)

        for ch in range(1, self.MAX_CHANNELS + 1):
            self._ch_apply[ch].clicked.connect(self._make_apply_handler(ch))
            self._ch_on_btn[ch].clicked.connect(self._make_output_handler(ch, True))
            self._ch_off_btn[ch].clicked.connect(self._make_output_handler(ch, False))
            self._ch_arm[ch].toggled.connect(self._make_arm_handler(ch))
            self._prot_apply[ch].clicked.connect(self._make_protection_handler(ch))

        self._signals.status.connect(self._log_msg)
        self._signals.connected.connect(self._on_connect_result)
        self._signals.measure_done.connect(self._on_measure_done)
        self._signals.status_done.connect(self._on_status_done)

    # ------------------------------------------------------------------
    # Connection slots
    # ------------------------------------------------------------------

    def _on_connect(self):
        self._ctrl = NGE100Controller(
            resource=self._resource_edit.text().strip(),
            mode=self._mode_combo.currentText(),
        )
        self._log_msg("Connecting...")
        self._connect_btn.setEnabled(False)
        w = _ConnectWorker(self._ctrl, self._signals)
        w.start(); self._worker = w

    def _on_connect_result(self, ok: bool, msg: str):
        self._connect_btn.setEnabled(True)
        if ok:
            self._conn_label.setText(f"Connected: {msg}")
            self._conn_label.setStyleSheet("color: green; font-weight: bold;")
            self._disconnect_btn.setEnabled(True)
            self._num_channels = self._ctrl.num_channels if self._ctrl else self.MAX_CHANNELS

            # Enable/disable widgets per available channel
            for ch in range(1, self.MAX_CHANNELS + 1):
                enable = ch <= self._num_channels
                for widget in (self._ch_apply[ch], self._ch_on_btn[ch],
                               self._ch_off_btn[ch], self._ch_arm[ch],
                               self._prot_apply[ch]):
                    widget.setEnabled(enable)
            self._master_on_btn.setEnabled(True)
            self._master_off_btn.setEnabled(True)
            self._refresh_btn.setEnabled(True)
            self._poll_timer.start()
        else:
            self._conn_label.setText("Failed")
            self._conn_label.setStyleSheet("color: red; font-weight: bold;")
            self._ctrl = None
        self._log_msg(("Connected: " if ok else "FAILED: ") + msg)

    def _on_disconnect(self):
        self._poll_timer.stop()
        if self._ctrl:
            try: self._ctrl.disconnect()
            except Exception as e: self._log_msg(f"Disconnect: {e}")
            self._ctrl = None
        self._conn_label.setText("Not connected")
        self._conn_label.setStyleSheet("color: red; font-weight: bold;")
        self._disconnect_btn.setEnabled(False)
        for ch in range(1, self.MAX_CHANNELS + 1):
            for widget in (self._ch_apply[ch], self._ch_on_btn[ch],
                           self._ch_off_btn[ch], self._ch_arm[ch],
                           self._prot_apply[ch]):
                widget.setEnabled(False)
            self._ch_state[ch].setText("Output: OFF")
            self._ch_state[ch].setStyleSheet("color: red;")
            self._ch_meas[ch].setText("— V    — A    — W")
        self._master_on_btn.setEnabled(False)
        self._master_off_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._log_msg("Disconnected.")

    def _on_test(self):
        config = {"resource": self._resource_edit.text().strip(),
                  "mode":     self._mode_combo.currentText()}

        class _T(QThread):
            done = pyqtSignal(bool, str)
            def run(self_):
                ok, msg = NGE100Controller.test(config)
                self_.done.emit(ok, msg)

        t = _T(self)
        t.done.connect(lambda ok, m: self._log_msg(
            f"Test {'OK' if ok else 'FAILED'}: {m}"))
        t.start(); self._worker = t

    def _on_discover(self):
        class _D(QThread):
            done = pyqtSignal(object)
            def run(self_):
                self_.done.emit(NGE100Controller.discover())

        def _show(found):
            if not found:
                self._log_msg("Discover: no R&S NGE devices found.")
                return
            for res, idn in found:
                self._log_msg(f"Found: {res}  →  {idn}")
            self._resource_edit.setText(found[0][0])

        d = _D(self)
        d.done.connect(_show)
        d.start(); self._worker = d

    # ------------------------------------------------------------------
    # Per-channel slots
    # ------------------------------------------------------------------

    def _make_apply_handler(self, ch: int):
        def handler():
            if not self._ctrl: return
            v = self._ch_volt[ch].value()
            i = self._ch_curr[ch].value()
            try:
                self._ctrl.apply(ch, v, i)
                self._log_msg(f"CH{ch}: apply {v:.3f} V / {i:.3f} A")
            except Exception as e:
                self._log_msg(f"CH{ch} apply error: {e}")
        return handler

    def _make_output_handler(self, ch: int, on: bool):
        def handler():
            if not self._ctrl: return
            try:
                (self._ctrl.output_on if on else self._ctrl.output_off)(ch)
                self._ch_state[ch].setText(f"Output: {'ON' if on else 'OFF'}")
                self._ch_state[ch].setStyleSheet(
                    "color: green;" if on else "color: red;")
                self._log_msg(f"CH{ch}: output {'ON' if on else 'OFF'}")
            except Exception as e:
                self._log_msg(f"CH{ch} output error: {e}")
        return handler

    def _make_arm_handler(self, ch: int):
        def handler(checked: bool):
            if not self._ctrl: return
            try:
                self._ctrl.select_channel(ch, checked)
                self._log_msg(f"CH{ch}: {'armed' if checked else 'disarmed'}")
            except Exception as e:
                self._log_msg(f"CH{ch} arm error: {e}")
        return handler

    def _make_protection_handler(self, ch: int):
        def handler():
            if not self._ctrl: return
            try:
                self._ctrl.set_ovp(ch, self._prot_ovp_lvl[ch].value(),
                                   self._prot_ovp_en[ch].isChecked())
                self._ctrl.set_opp(ch, self._prot_opp_lvl[ch].value(),
                                   self._prot_opp_en[ch].isChecked())
                self._ctrl.set_fuse(ch, self._prot_fuse_en[ch].isChecked(),
                                    delay_ms=self._prot_fuse_del[ch].value())
                self._ctrl.set_easyramp(ch, self._prot_ramp_dur[ch].value(),
                                        self._prot_ramp_en[ch].isChecked())
                self._log_msg(f"CH{ch}: protection updated")
            except Exception as e:
                self._log_msg(f"CH{ch} protection error: {e}")
        return handler

    # ------------------------------------------------------------------
    # Master output
    # ------------------------------------------------------------------

    def _on_master_on(self):
        if not self._ctrl: return
        try:
            self._ctrl.master_output_on()
            self._log_msg("Master output ON")
        except Exception as e:
            self._log_msg(f"Master ON error: {e}")

    def _on_master_off(self):
        if not self._ctrl: return
        try:
            self._ctrl.master_output_off()
            self._log_msg("Master output OFF")
        except Exception as e:
            self._log_msg(f"Master OFF error: {e}")

    # ------------------------------------------------------------------
    # Measurement polling
    # ------------------------------------------------------------------

    def _on_tab_changed(self, _idx: int):
        # Could pause polling on tabs that don't need it; leave running for now.
        pass

    def _poll_measurements(self):
        if not self._ctrl or not self._ctrl.is_connected:
            return
        # Don't queue multiple measurement workers; pick one per tick.
        if self._worker is not None and self._worker.isRunning():
            return
        for ch in range(1, self._num_channels + 1):
            w = _MeasureWorker(self._ctrl, ch, self._signals)
            w.start(); self._worker = w

    def _on_measure_done(self, ch: int, meas: dict):
        v = meas.get("voltage"); i = meas.get("current"); p = meas.get("power")
        def _fmt(x, unit):
            return f"{x:.3f} {unit}" if x is not None else f"— {unit}"
        self._ch_meas[ch].setText(
            f"{_fmt(v, 'V')}    {_fmt(i, 'A')}    {_fmt(p, 'W')}"
        )

    # ------------------------------------------------------------------
    # Status tab
    # ------------------------------------------------------------------

    def _on_refresh_status(self):
        if not self._ctrl: return
        w = _StatusWorker(self._ctrl, self._signals)
        w.start(); self._worker = w

    def _on_status_done(self, st: InstrumentStatus):
        lines = []
        lines.append(f"Connected   : {st.connected}")
        lines.append(f"IDN         : {st.idn}")
        lines.append(f"Channels    : {st.num_channels}")
        for ch, cs in st.channels.items():
            lines.append("")
            lines.append(f"--- Channel {ch} ---")
            lines.append(f"  Output    : {'ON' if cs.output_on else 'OFF'}  "
                         f"(armed: {'YES' if cs.selected else 'no'})")
            lines.append(f"  Setpoint  : {cs.voltage_set:.3f} V  /  {cs.current_set:.3f} A")
            lines.append(f"  Measured  : {cs.measured_voltage:.3f} V  "
                         f"{cs.measured_current:.3f} A  {cs.measured_power:.3f} W")
            lines.append(f"  OVP       : {'ON' if cs.ovp_enabled else 'OFF'} "
                         f"@ {cs.ovp_level:.2f} V "
                         f"({'TRIPPED' if cs.ovp_tripped else 'ok'})")
            lines.append(f"  OPP       : {'ON' if cs.opp_enabled else 'OFF'} "
                         f"@ {cs.opp_level:.2f} W "
                         f"({'TRIPPED' if cs.opp_tripped else 'ok'})")
            lines.append(f"  Fuse      : {'ON' if cs.fuse_enabled else 'OFF'} "
                         f"delay {cs.fuse_delay_ms:.0f} ms "
                         f"({'TRIPPED' if cs.fuse_tripped else 'ok'})")
        self._status_text.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _log_msg(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log.append(f"[{ts}] {msg}")

    def closeEvent(self, event):
        self._poll_timer.stop()
        if self._ctrl:
            try: self._ctrl.disconnect()
            except Exception: pass
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    win = NGE100Window()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
