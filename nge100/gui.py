"""
nge100/gui.py

NiceGUI control panel for the R&S NGE100-series DC power supply.

Same standalone-or-embedded pattern as the other Brunner-neutrino-lab
instrument GUIs (b2987b, vx2740, pulse_mux, phidget_stage,
keithley6485, dg1022).
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from nicegui import ui

from .controller import NGE100Controller
from .driver     import DEFAULT_RESOURCE


# ---------------------------------------------------------------------------
# Style — xsphere/DAQ palette
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg:#11151c; --panel:#1b2230; --panel2:#232c3d;
  --fg:#dde3ee; --mut:#8a93a6;
  --ok:#3fb950; --warn:#d29922; --bad:#f85149; --acc:#58a6ff;
  --line:#2d3648;
}
html, body, .nicegui-content { background:var(--bg) !important; color:var(--fg);
  font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; }
.pill { padding:.15rem .55rem; border-radius:999px; font-size:.78rem;
  font-weight:600; white-space:nowrap; display:inline-flex; align-items:center; gap:.3rem; }
.pill.ok   { background:rgba(63,185,80,.18);  color:var(--ok); }
.pill.bad  { background:rgba(248,81,73,.18);  color:var(--bad); }
.pill.warn { background:rgba(210,153,34,.18); color:var(--warn); }
.pill.mut  { background:rgba(138,147,166,.15);color:var(--mut); }
.q-card, .nge-card {
  background:var(--panel) !important; color:var(--fg) !important;
  border:1px solid var(--line); border-radius:10px;
  box-shadow:none !important; padding:.55rem .85rem .7rem !important;
}
.nge-card h2 { font-size:.92rem; margin:.05rem 0 .45rem; color:var(--acc);
  font-weight:600; letter-spacing:.3px; }
.q-btn { background:var(--panel2) !important; color:var(--fg) !important;
  border:1px solid var(--line) !important; border-radius:6px !important;
  box-shadow:none !important; padding:.18rem .65rem !important;
  min-height:32px !important; text-transform:none !important; }
.q-btn:hover { border-color:var(--acc) !important; }
.q-btn[data-q-color="primary"], .q-btn.bg-primary {
  background:var(--acc) !important; color:#08111f !important;
  border-color:var(--acc) !important; font-weight:600 !important; }
.q-btn[data-q-color="negative"], .q-btn.bg-negative {
  background:transparent !important; color:var(--bad) !important;
  border-color:var(--bad) !important; }
.q-field__control, .q-field--filled .q-field__control {
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:6px !important; min-height:32px !important; color:var(--fg) !important; }
.q-field__label, .q-field__native, .q-field input { color:var(--fg) !important; }
.q-field__label { color:var(--mut) !important; }
.q-field--filled .q-field__control:before,
.q-field--filled .q-field__control:after { display:none !important; }
.q-log, .nicegui-log { background:var(--panel2) !important; color:var(--fg) !important;
  border:1px solid var(--line); border-radius:6px;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.82rem; }
.num { font-variant-numeric:tabular-nums; }
"""


async def _in_thread(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)


def _channel_card(ch: int, get_ctrl, log_msg):
    """Build one per-channel control card."""
    with ui.card().classes("nge-card"):
        ui.html(f"<h2>channel {ch}</h2>")

        v_set = ui.number(label="V setpoint (V)", value=5.0, step=0.1,
                          format="%.3f").classes("w-36 num")
        i_set = ui.number(label="I limit (A)", value=0.5, step=0.01,
                          format="%.3f").classes("w-36 num")
        out_pill = ui.html(f'<span class="pill mut">ch{ch} output: off</span>')
        meas_v   = ui.label("V meas: —").classes("num text-sm")
        meas_i   = ui.label("I meas: —").classes("num text-sm")
        meas_p   = ui.label("P meas: —").classes("num text-sm")
        status   = ui.label("").classes("text-xs text-gray-400")

        async def apply_setp():
            c = get_ctrl()
            if c is None: log_msg(f"ch{ch} apply: not connected"); return
            try:
                ok = await _in_thread(c.apply, ch, float(v_set.value), float(i_set.value))
                log_msg(f"ch{ch} apply  V={v_set.value} V  I={i_set.value} A  → {ok}")
            except Exception as e:
                log_msg(f"ch{ch} apply FAIL: {type(e).__name__}: {e}")

        async def out_on():
            c = get_ctrl()
            if c is None: log_msg(f"ch{ch} output_on: not connected"); return
            try:
                await _in_thread(c.output_on, ch)
                out_pill.content = f'<span class="pill ok">ch{ch} output: on</span>'
                log_msg(f"ch{ch} output ON")
            except Exception as e:
                log_msg(f"ch{ch} output_on FAIL: {type(e).__name__}: {e}")

        async def out_off():
            c = get_ctrl()
            if c is None: log_msg(f"ch{ch} output_off: not connected"); return
            try:
                await _in_thread(c.output_off, ch)
                out_pill.content = f'<span class="pill mut">ch{ch} output: off</span>'
                log_msg(f"ch{ch} output OFF")
            except Exception as e:
                log_msg(f"ch{ch} output_off FAIL: {type(e).__name__}: {e}")

        async def measure():
            c = get_ctrl()
            if c is None: log_msg(f"ch{ch} measure: not connected"); return
            try:
                m = await _in_thread(c.measure_all, ch)
                if m is None:
                    status.text = "measure returned None"; return
                v = m.get("voltage"); i = m.get("current"); p = m.get("power")
                meas_v.text = f"V meas: {v:+.3f} V" if v is not None else "V meas: —"
                meas_i.text = f"I meas: {i:+.4f} A" if i is not None else "I meas: —"
                meas_p.text = f"P meas: {p:+.4f} W" if p is not None else "P meas: —"
                bits = []
                for fname, label in (("is_ovp_tripped", "OVP tripped"),
                                       ("is_opp_tripped", "OPP tripped"),
                                       ("is_fuse_tripped","fuse tripped")):
                    try:
                        f = getattr(c, fname, None)
                        if f is not None and f(ch):
                            bits.append(label)
                    except Exception:
                        pass
                status.text = " · ".join(bits) if bits else ""
            except Exception as e:
                log_msg(f"ch{ch} measure FAIL: {type(e).__name__}: {e}")

        with ui.row().classes("gap-2 mt-1"):
            ui.button("apply",   on_click=apply_setp).props("color=primary")
            ui.button("on",      on_click=out_on).props("color=primary")
            ui.button("off",     on_click=out_off).props("color=negative")
            ui.button("measure", on_click=measure)


# ===========================================================================
# build_page
# ===========================================================================

def build_page(get_controller: Optional[Callable[[], Optional[NGE100Controller]]] = None,
               *, show_connection: Optional[bool] = None) -> None:
    """Render the NGE100 control panel into the current container."""
    if show_connection is None:
        show_connection = (get_controller is None)

    _own = {"ctrl": None}
    if get_controller is None:
        def get_controller():
            return _own["ctrl"]

    log = ui.log(max_lines=160).classes("h-32 w-full")
    def log_msg(s: str): log.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):

        if show_connection:
            with ui.card().classes("nge-card"):
                ui.html("<h2>connection</h2>")
                res_in  = ui.input(label="resource",
                                   value=DEFAULT_RESOURCE).classes("w-72 num")
                mode_in = ui.select(["simulation", "hardware"],
                                    value="simulation", label="mode").classes("w-40")
                conn_pill = ui.html('<span class="pill mut">disconnected</span>')

                def set_pill(text: str, cls: str):
                    conn_pill.content = f'<span class="pill {cls}">{text}</span>'

                async def do_connect():
                    c = NGE100Controller(resource=res_in.value.strip(),
                                         mode=mode_in.value)
                    set_pill("connecting…", "warn")
                    try:
                        await _in_thread(c.connect)
                        _own["ctrl"] = c
                        idn = c.idn() or c.identify()
                        set_pill(f"OK — {str(idn)[:60]}", "ok")
                        log_msg(f"connected: {idn}")
                    except Exception as e:
                        set_pill(f"FAIL: {type(e).__name__}", "bad")
                        log_msg(f"connect FAIL: {type(e).__name__}: {e}")

                async def do_disconnect():
                    c = _own["ctrl"]
                    if c is None: return
                    try: await _in_thread(c.disconnect)
                    except Exception as e: log_msg(f"disconnect warn: {e}")
                    _own["ctrl"] = None
                    set_pill("disconnected", "mut")
                    log_msg("disconnected")

                with ui.row().classes("mt-1 gap-2"):
                    ui.button("connect",    on_click=do_connect).props("color=primary")
                    ui.button("disconnect", on_click=do_disconnect).props("color=negative flat")

        with ui.card().classes("nge-card"):
            ui.html("<h2>master</h2>")
            nch_lbl  = ui.label("channels: —").classes("num text-sm")
            mst_pill = ui.html('<span class="pill mut">master output: ?</span>')

            async def all_on():
                c = get_controller()
                if c is None: log_msg("not connected"); return
                try:
                    await _in_thread(c.all_outputs_on)
                    mst_pill.content = '<span class="pill ok">all outputs on</span>'
                    log_msg("all outputs ON")
                except Exception as e:
                    log_msg(f"all_outputs_on FAIL: {type(e).__name__}: {e}")

            async def all_off():
                c = get_controller()
                if c is None: log_msg("not connected"); return
                try:
                    await _in_thread(c.all_outputs_off)
                    mst_pill.content = '<span class="pill mut">all outputs off</span>'
                    log_msg("all outputs OFF")
                except Exception as e:
                    log_msg(f"all_outputs_off FAIL: {type(e).__name__}: {e}")

            async def refresh_nch():
                c = get_controller()
                if c is None: log_msg("not connected"); return
                try:
                    n = await _in_thread(lambda: c.num_channels())
                    nch_lbl.text = f"channels: {n}"
                except Exception as e:
                    log_msg(f"num_channels FAIL: {type(e).__name__}: {e}")

            with ui.row().classes("gap-2 mt-1"):
                ui.button("all outputs ON",  on_click=all_on).props("color=primary")
                ui.button("all outputs OFF", on_click=all_off).props("color=negative")
                ui.button("refresh", on_click=refresh_nch)

    # Per-channel cards
    with ui.row().classes("w-full gap-3 items-start mt-2"):
        for ch in (1, 2, 3):
            _channel_card(ch, get_controller, log_msg)


# ---------------------------------------------------------------------------
# Standalone entry — `python -m nge100.gui`
# ---------------------------------------------------------------------------

def main():
    import argparse
    p = argparse.ArgumentParser(description="R&S NGE100 web GUI")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8772)
    args = p.parse_args()

    @ui.page("/")
    def index():
        ui.add_head_html(f"<style>{_CSS}</style>")
        ui.dark_mode().enable()
        with ui.element("header").style(
            "display:flex;align-items:center;gap:.8rem;"
            "padding:.55rem 1rem;background:var(--panel);"
            "border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5"
        ):
            ui.html("<h1 style='font-size:1.05rem;font-weight:600;margin:0'>"
                    "R&amp;S NGE100 · power supply</h1>")
        build_page()

    ui.run(host=args.host, port=args.port, reload=False,
           title="NGE100 PSU", show=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
