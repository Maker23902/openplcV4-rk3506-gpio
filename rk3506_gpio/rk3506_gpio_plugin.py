#!/usr/bin/env python3
"""
RK3506 GPIO plugin for OpenPLC Runtime V4 desktop.

Maps RK3506 physical GPIO pins to IEC 61131-3 PLC addresses
so they can be read and driven from any PLC program running in the runtime.

GPIO mapping
------------
RK3506 GPIO naming convention:
    GPIO{bank}_{group}{n}
    Linux kernel chip  : gpiochip{bank}
    Line offset        : group_index * 8 + n
    Groups             : A=0, B=1, C=2, D=3

Outputs (%QX -> GPIO):
    %QX0.0 -> GPIO1_B3  (chip=1, offset=11)
    %QX0.1 -> GPIO1_B4  (chip=1, offset=12)
    %QX0.2 -> GPIO1_B5  (chip=1, offset=13)
    %QX0.3 -> GPIO1_B6  (chip=1, offset=14)

Inputs (GPIO -> %IX):
    GPIO1_C0 (chip=1, offset=16) -> %IX0.0
    GPIO1_C1 (chip=1, offset=17) -> %IX0.1
    GPIO1_C2 (chip=1, offset=18) -> %IX0.2
    GPIO1_C3 (chip=1, offset=19) -> %IX0.3
    GPIO1_C4 (chip=1, offset=20) -> %IX0.4
    GPIO1_C5 (chip=1, offset=21) -> %IX0.5
    GPIO1_C6 (chip=1, offset=22) -> %IX0.6
    GPIO1_C7 (chip=1, offset=23) -> %IX0.7

Hardware notes
--------------
* All input pins are assumed to have external pull-down resistors wired in
  hardware. No internal bias is requested from the kernel (BIAS_DISABLE).
* Output pins are driven LOW on startup and on cleanup.
* Access is performed via libgpiod (python3-gpiod).  Install with:
      sudo apt install python3-gpiod        # Debian / Ubuntu
      pip install gpiod                     # PyPI wheel
  The gpiod Python bindings require libgpiod >= 1.6 on the system.

Polling
-------
The background thread runs every POLL_INTERVAL seconds (default 10 ms = 100 Hz).
On each tick it:
    1. Reads the physical state of all input lines and writes the values to
       the PLC %IX buffer.
    2. Reads the PLC %QX buffer and drives the corresponding output lines.

If the GPIO chip device cannot be opened at startup the thread retries the
connection every second until it succeeds.

Compatibility
-------------
Tested with libgpiod 1.6 / gpiod Python package 1.5.x and 2.x.
The plugin prefers the gpiod v2 API (chip.request_lines) but falls back
to the v1 API (chip.get_lines) automatically.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared import PluginLogger, SafeBufferAccess, safe_extract_runtime_args_from_capsule

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Poll interval in seconds (10 ms -> 100 Hz)
POLL_INTERVAL = 0.010

# Path to the gpiochip device used by both input and output pins.
# On RK3506 with a vanilla Rockchip kernel the banks are exposed as:
#   gpiochip0 … gpiochip4
# All pins in this default mapping live on gpiochip1.
GPIO_CHIP = "gpiochip1"

# Consumer tag shown in `gpioinfo` output while the plugin holds the lines
CONSUMER_TAG = "openplc-rk3506"

# ---------------------------------------------------------------------------
# Pin maps
# ---------------------------------------------------------------------------
# INPUT_MAP  : list of (line_offset, buf_idx, bit_idx)
#              line_offset is the offset within GPIO_CHIP
#              buf_idx / bit_idx address the PLC %IX byte and bit
#
# OUTPUT_MAP : list of (buf_idx, bit_idx, line_offset)
#              buf_idx / bit_idx address the PLC %QX byte and bit

INPUT_MAP = [
    (16, 0, 0),  # GPIO1_C0 -> %IX0.0
    (17, 0, 1),  # GPIO1_C1 -> %IX0.1
    (18, 0, 2),  # GPIO1_C2 -> %IX0.2
    (19, 0, 3),  # GPIO1_C3 -> %IX0.3
    (20, 0, 4),  # GPIO1_C4 -> %IX0.4
    (21, 0, 5),  # GPIO1_C5 -> %IX0.5
    (22, 0, 6),  # GPIO1_C6 -> %IX0.6
    (23, 0, 7),  # GPIO1_C7 -> %IX0.7
]

OUTPUT_MAP = [
    (0, 0, 11),  # %QX0.0 -> GPIO1_B3
    (0, 1, 12),  # %QX0.1 -> GPIO1_B4
    (0, 2, 13),  # %QX0.2 -> GPIO1_B5
    (0, 3, 14),  # %QX0.3 -> GPIO1_B6
]

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_runtime_args = None
_safe_buffer: SafeBufferAccess = None
_logger: PluginLogger = None

_input_lines  = None   # gpiod line handle(s) for inputs
_output_lines = None   # gpiod line handle(s) for outputs
_gpiod_v2     = False  # True when the v2 gpiod API is available

_stop_event = threading.Event()
_thread: threading.Thread = None


# ---------------------------------------------------------------------------
# gpiod helpers – abstract away v1 / v2 API differences
# ---------------------------------------------------------------------------

def _detect_gpiod_version():
    """Return True if the installed gpiod package exposes the v2 API."""
    try:
        import gpiod
        return hasattr(gpiod, "request_lines")   # v2 top-level helper
    except ImportError:
        return False


def _open_lines():
    """
    Open and request the GPIO lines defined in INPUT_MAP and OUTPUT_MAP.

    Returns (input_lines, output_lines, is_v2) on success, or raises an
    exception that the caller should catch and log.
    """
    import gpiod

    in_offsets  = [offset for offset, _, _ in INPUT_MAP]
    out_offsets = [offset for _, _, offset in OUTPUT_MAP]

    # --- gpiod v2 API (gpiod >= 2.0 / python gpiod >= 2.0) ---
    if hasattr(gpiod, "request_lines"):
        chip_path = f"/dev/{GPIO_CHIP}"

        # Input lines: bias disabled (external pull-downs in hardware)
        input_lines = gpiod.request_lines(
            chip_path,
            consumer=CONSUMER_TAG,
            config={
                tuple(in_offsets): gpiod.LineSettings(
                    direction=gpiod.line.Direction.INPUT,
                    bias=gpiod.line.Bias.DISABLED,
                )
            },
        )

        # Output lines: start all LOW
        output_lines = gpiod.request_lines(
            chip_path,
            consumer=CONSUMER_TAG,
            config={
                tuple(out_offsets): gpiod.LineSettings(
                    direction=gpiod.line.Direction.OUTPUT,
                    output_value=gpiod.line.Value.INACTIVE,
                )
            },
        )
        return input_lines, output_lines, True

    # --- gpiod v1 API (gpiod 1.5.x) ---
    chip = gpiod.Chip(GPIO_CHIP)

    input_lines = chip.get_lines(in_offsets)
    input_lines.request(
        consumer=CONSUMER_TAG,
        type=gpiod.LINE_REQ_DIR_IN,
        flags=gpiod.LINE_REQ_FLAG_BIAS_DISABLE,
    )

    output_lines = chip.get_lines(out_offsets)
    output_lines.request(
        consumer=CONSUMER_TAG,
        type=gpiod.LINE_REQ_DIR_OUT,
        default_vals=[0] * len(out_offsets),
    )

    return input_lines, output_lines, False


def _read_inputs_v2(lines) -> list:
    """Return a flat list of integer values [0|1] for INPUT_MAP order (v2)."""
    offsets = [offset for offset, _, _ in INPUT_MAP]
    vals = lines.get_values(offsets)
    # v2 get_values returns gpiod.line.Value enum members; convert to int
    return [int(v) for v in vals]


def _read_inputs_v1(lines) -> list:
    """Return a flat list of integer values [0|1] for INPUT_MAP order (v1)."""
    return list(lines.get_values())


def _write_outputs_v2(lines, values: list):
    """Drive output lines according to `values` [0|1] in OUTPUT_MAP order (v2)."""
    import gpiod
    offsets = [offset for _, _, offset in OUTPUT_MAP]
    enum_vals = [
        gpiod.line.Value.ACTIVE if v else gpiod.line.Value.INACTIVE
        for v in values
    ]
    lines.set_values(dict(zip(offsets, enum_vals)))


def _write_outputs_v1(lines, values: list):
    """Drive output lines according to `values` [0|1] in OUTPUT_MAP order (v1)."""
    lines.set_values(values)


def _release_lines():
    """Release both line handles, suppressing any errors."""
    global _input_lines, _output_lines

    for handle in (_input_lines, _output_lines):
        if handle is not None:
            try:
                handle.release()
            except Exception:
                pass

    _input_lines = None
    _output_lines = None


def _drive_all_outputs_low():
    """Set all output pins to 0 without touching the PLC buffer."""
    if _output_lines is None:
        return
    try:
        zeros = [0] * len(OUTPUT_MAP)
        if _gpiod_v2:
            _write_outputs_v2(_output_lines, zeros)
        else:
            _write_outputs_v1(_output_lines, zeros)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background polling thread
# ---------------------------------------------------------------------------

def _poll_loop():
    """
    Background thread body.

    Runs every POLL_INTERVAL seconds. If the GPIO lines are not yet open
    (e.g. gpiochip device not ready at plugin start), attempts to open them
    and retries every second until successful.
    """
    global _input_lines, _output_lines, _gpiod_v2

    while not _stop_event.is_set():

        # ---- Reconnect if lines are not held ----
        if _input_lines is None or _output_lines is None:
            try:
                _input_lines, _output_lines, _gpiod_v2 = _open_lines()
                _logger.info(
                    f"GPIO lines acquired on {GPIO_CHIP} "
                    f"(gpiod API v{'2' if _gpiod_v2 else '1'})"
                )
            except Exception as exc:
                _logger.error(f"Cannot open GPIO lines: {exc} — retrying in 1 s")
                _release_lines()
                _stop_event.wait(1.0)
                continue

        try:
            # ---- Read inputs: physical GPIO -> PLC %IX ----
            if _gpiod_v2:
                raw_inputs = _read_inputs_v2(_input_lines)
            else:
                raw_inputs = _read_inputs_v1(_input_lines)

            for idx, (_, buf_idx, bit_idx) in enumerate(INPUT_MAP):
                pin_val = bool(raw_inputs[idx])
                _, err = _safe_buffer.write_bool_input(buf_idx, bit_idx, pin_val)
                if err != "Success":
                    offset = INPUT_MAP[idx][0]
                    _logger.error(
                        f"write_bool_input offset={offset} "
                        f"-> %IX{buf_idx}.{bit_idx}: {err}"
                    )

            # ---- Write outputs: PLC %QX -> physical GPIO ----
            _safe_buffer.acquire_mutex()
            try:
                out_values = []
                for buf_idx, bit_idx, offset in OUTPUT_MAP:
                    val, err = _safe_buffer.read_bool_output(
                        buf_idx, bit_idx, thread_safe=False
                    )
                    if err == "Success":
                        out_values.append(1 if val else 0)
                    else:
                        _logger.error(
                            f"read_bool_output %QX{buf_idx}.{bit_idx}: {err}"
                        )
                        out_values.append(0)   # Safe default: drive LOW

                if _gpiod_v2:
                    _write_outputs_v2(_output_lines, out_values)
                else:
                    _write_outputs_v1(_output_lines, out_values)
            finally:
                _safe_buffer.release_mutex()

        except Exception as exc:
            _logger.error(f"GPIO poll error: {exc}")
            _release_lines()   # Force reconnect on next iteration

        _stop_event.wait(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# OpenPLC Runtime V4 plugin contract
# ---------------------------------------------------------------------------
# The runtime calls these four functions in order:
#   init(capsule)  -> once when the plugin is loaded
#   start_loop()   -> when the PLC program starts
#   stop_loop()    -> when the PLC program stops
#   cleanup()      -> when the runtime shuts down

def init(runtime_args_capsule):
    """Extract runtime arguments and initialise the shared-buffer handle."""
    global _runtime_args, _safe_buffer, _logger

    _logger = PluginLogger("RK3506_GPIO", None)
    _logger.info("rk3506_gpio plugin initialising...")

    try:
        runtime_args, err = safe_extract_runtime_args_from_capsule(
            runtime_args_capsule
        )
        if runtime_args is None:
            _logger.error(f"Failed to extract runtime args: {err}")
            return False

        _logger = PluginLogger("RK3506_GPIO", runtime_args)
        _runtime_args = runtime_args

        _safe_buffer = SafeBufferAccess(runtime_args)
        if not _safe_buffer.is_valid:
            _logger.error(f"SafeBufferAccess invalid: {_safe_buffer.error_msg}")
            return False

        # Verify that gpiod is importable so we fail fast with a clear message
        try:
            import gpiod  # noqa: F401
        except ImportError:
            _logger.error(
                "Python package 'gpiod' not found. "
                "Install with: sudo apt install python3-gpiod  "
                "or: pip install gpiod"
            )
            return False

        _logger.info("rk3506_gpio plugin initialised")
        return True

    except Exception as exc:
        _logger.error(f"Initialisation error: {exc}")
        import traceback
        traceback.print_exc()
        return False


def start_loop():
    """Open GPIO lines and launch the polling thread."""
    global _input_lines, _output_lines, _gpiod_v2, _thread

    if _runtime_args is None:
        _logger.error("Plugin not initialised — cannot start loop")
        return False

    # Attempt an immediate open; if it fails the thread will keep retrying
    try:
        _input_lines, _output_lines, _gpiod_v2 = _open_lines()
        _logger.info(
            f"GPIO lines acquired on {GPIO_CHIP} "
            f"(gpiod API v{'2' if _gpiod_v2 else '1'})"
        )
    except Exception as exc:
        _logger.warn(
            f"Initial GPIO open failed ({exc}) — "
            "poll thread will retry every second"
        )

    _stop_event.clear()
    _thread = threading.Thread(
        target=_poll_loop, daemon=True, name="rk3506_gpio_poll"
    )
    _thread.start()
    _logger.info(
        f"GPIO polling thread started (interval={int(POLL_INTERVAL * 1000)} ms)"
    )
    return 0


def stop_loop():
    """Signal the polling thread to stop and wait for it to exit."""
    global _thread

    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
        _thread = None
    _logger.info("GPIO polling thread stopped")
    return True


def cleanup():
    """Drive all outputs LOW, release GPIO lines, and free resources."""
    global _runtime_args, _safe_buffer

    _drive_all_outputs_low()
    _release_lines()

    _runtime_args = None
    _safe_buffer = None
    _logger.info("rk3506_gpio plugin cleaned up")
    return True
