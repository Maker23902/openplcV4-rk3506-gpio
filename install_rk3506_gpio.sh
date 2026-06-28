#!/bin/sh
# ============================================================
#  OpenPLC Runtime V4 — RK3506 GPIO Plugin Installer
#  Target: Buildroot 2024.02, Linux kernel 6.1, BusyBox init
# ============================================================
#
#  Prerequisites (must be added to your Buildroot configuration):
#    BR2_PACKAGE_PYTHON3=y
#    BR2_PACKAGE_PYTHON3_GPIOD=y   # python3-gpiod (libgpiod Python bindings)
#    BR2_PACKAGE_LIBGPIOD=y        # libgpiod C library (pulled in automatically)
#
#  Usage (copy to target and run as root):
#    chmod +x install_rk3506_gpio.sh
#    ./install_rk3506_gpio.sh
#
#  GPIO mapping installed by this script:
#    Outputs (gpiochip1):  offset 11=%QX0.0  offset 12=%QX0.1
#                          offset 13=%QX0.2  offset 14=%QX0.3
#    Inputs  (gpiochip1):  offset 16=%IX0.0  offset 17=%IX0.1
#                          offset 18=%IX0.2  offset 19=%IX0.3
#                          offset 20=%IX0.4  offset 21=%IX0.5
#                          offset 22=%IX0.6  offset 23=%IX0.7
#  RK3506 naming:
#    GPIO1_B3=offset11 … GPIO1_B6=offset14 (outputs)
#    GPIO1_C0=offset16 … GPIO1_C7=offset23 (inputs)
# ============================================================

set -eu

PLUGIN_NAME="rk3506_gpio"
PLUGIN_FILE="rk3506_gpio.py"
GITHUB_RAW="https://raw.githubusercontent.com/Maker23902/openplcV4-rk3506-gpio/refs/heads"

# Kernel 6.1 + libgpiod v2 — chip device path prefix
GPIOCHIP_PATH="/dev/gpiochip1"

echo "============================================"
echo " OpenPLC V4 — RK3506 GPIO Plugin Installer"
echo " Target: Buildroot 2024.02 / kernel 6.1"
echo "============================================"
echo ""

# ── Step 0: Must run as root ──────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run this script as root."
    exit 1
fi

# ── Step 1: Locate OpenPLC installation ──────────────────────────────────────
echo "[1/7] Locating OpenPLC Runtime V4 installation..."

# Buildroot does not use systemd; look for the init script to find the path.
# Convention: /etc/init.d/S90openplc contains "OPENPLC_DIR=/opt/openplc" or similar.
OPENPLC_DIR=""

# Strategy A: parse the init script
INIT_SCRIPT="/etc/init.d/S90openplc"
if [ -f "$INIT_SCRIPT" ]; then
    OPENPLC_DIR=$(grep -E "^OPENPLC_DIR=" "$INIT_SCRIPT" 2>/dev/null \
        | head -1 | cut -d= -f2 | tr -d '"' | tr -d "'")
fi

# Strategy B: well-known default paths
if [ -z "$OPENPLC_DIR" ]; then
    for candidate in /opt/openplc /opt/openplcruntime /home/openplc; do
        if [ -f "$candidate/plugins.conf" ]; then
            OPENPLC_DIR="$candidate"
            break
        fi
    done
fi

# Strategy C: search the filesystem (slow, last resort)
if [ -z "$OPENPLC_DIR" ]; then
    OPENPLC_DIR=$(find / -maxdepth 6 -name "plugins.conf" 2>/dev/null \
        | head -1 | xargs dirname 2>/dev/null || true)
fi

if [ -z "$OPENPLC_DIR" ] || [ ! -d "$OPENPLC_DIR" ]; then
    echo "ERROR: Cannot locate OpenPLC Runtime V4 directory."
    echo "  Set OPENPLC_DIR manually at the top of this script."
    exit 1
fi

PLUGIN_DIR="$OPENPLC_DIR/core/src/drivers/plugins/python/$PLUGIN_NAME"
PLUGINS_CONF="$OPENPLC_DIR/plugins.conf"
PLCAPP="$OPENPLC_DIR/webserver/plcapp_management.py"

echo "  Found OpenPLC at: $OPENPLC_DIR"

# ── Step 2: Check Python3 and gpiod bindings ─────────────────────────────────
echo "[2/7] Checking Python3 and gpiod availability..."

# Python3 — must be present in Buildroot rootfs
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found."
    echo "  Enable BR2_PACKAGE_PYTHON3=y in your Buildroot config and rebuild."
    exit 1
fi

PY_VER=$(python3 --version 2>&1)
echo "  Python: $PY_VER"

# gpiod Python bindings — installed at system level by Buildroot
if ! python3 -c "import gpiod" 2>/dev/null; then
    echo "ERROR: Python 'gpiod' module not found."
    echo "  Enable BR2_PACKAGE_PYTHON3_GPIOD=y in your Buildroot config and rebuild."
    echo "  (This pulls in libgpiod automatically.)"
    exit 1
fi

GPIOD_VER=$(python3 -c "import gpiod; print(gpiod.__version__)" 2>/dev/null || echo "unknown")
echo "  gpiod Python module: $GPIOD_VER"

# gpiochip device node
if [ ! -c "$GPIOCHIP_PATH" ]; then
    echo "ERROR: GPIO chip device not found: $GPIOCHIP_PATH"
    echo "  Check your device tree / pinmux configuration."
    exit 1
fi

echo "  GPIO chip device: $GPIOCHIP_PATH  OK"

# ── Step 3: Download plugin file ─────────────────────────────────────────────
echo "[3/7] Downloading plugin file..."

mkdir -p "$PLUGIN_DIR"

# Use wget (BusyBox built-in); fall back to curl if wget is not available
_download() {
    URL="$1"
    DEST="$2"
    if command -v wget >/dev/null 2>&1; then
        wget -q -O "$DEST" "$URL"
    elif command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" -o "$DEST"
    else
        echo "ERROR: Neither wget nor curl is available."
        exit 1
    fi
}

_download "$GITHUB_RAW/$PLUGIN_NAME/$PLUGIN_FILE" "$PLUGIN_DIR/$PLUGIN_FILE"
chmod 644 "$PLUGIN_DIR/$PLUGIN_FILE"

echo "  Plugin saved to: $PLUGIN_DIR/$PLUGIN_FILE"

# ── Step 4: Apply OpenPLC plcapp_management.py bugfix ────────────────────────
echo "[4/7] Applying OpenPLC V4 bugfix (plcapp_management.py)..."

# Bug: update_plugin_configurations() disables ALL enabled plugins whenever an
# uploaded PLC program ZIP contains no conf/ directory (the common case for
# hardware-driver plugins like this one, which have an empty config_path).
#
# Fix: skip the disable step for plugins that have no config_path, so hardware
# drivers survive every program upload without manual re-enabling.
#
# Changed pattern:
#   Before:  if plugin.enabled:
#   After:   if plugin.enabled and plugin.config_path:

if [ ! -f "$PLCAPP" ]; then
    echo "  WARNING: $PLCAPP not found — skipping bugfix."
else
    if grep -q "if plugin.enabled and plugin.config_path:" "$PLCAPP" 2>/dev/null; then
        echo "  Bugfix already applied, skipping."
    else
        python3 - <<PYEOF
import sys

path = "$PLCAPP"
try:
    with open(path, "r") as f:
        src = f.read()
except OSError as e:
    print("  WARNING: Cannot read {}: {}".format(path, e))
    sys.exit(0)

old = "if plugin.enabled:"
new = ("if plugin.enabled and plugin.config_path:"
       "  # patched: skip hardware drivers with empty config_path")

if old not in src:
    print("  WARNING: Expected pattern not found — skipping (file may differ).")
    sys.exit(0)

src = src.replace(old, new, 1)
with open(path, "w") as f:
    f.write(src)

print("  Bugfix applied successfully.")
PYEOF
    fi
fi

# ── Step 5: Register plugin in plugins.conf ───────────────────────────────────
echo "[5/7] Registering plugin in plugins.conf..."

# plugins.conf format (comma-separated, no spaces):
#   name, path, enabled(1/0), type(0=python), config_path, venv_path
#
# Buildroot has no per-plugin venv: Python packages are system-wide,
# so venv_path is left empty. The runtime must invoke the system python3.
PLUGIN_ENTRY="${PLUGIN_NAME},./core/src/drivers/plugins/python/${PLUGIN_NAME}/${PLUGIN_FILE},1,0,,"

if [ -f "$PLUGINS_CONF" ] && grep -q "^${PLUGIN_NAME}," "$PLUGINS_CONF" 2>/dev/null; then
    # Update existing entry in-place with BusyBox sed (no -i extension arg)
    TMP_CONF="${PLUGINS_CONF}.tmp"
    sed "s|^${PLUGIN_NAME},.*|${PLUGIN_ENTRY}|" "$PLUGINS_CONF" > "$TMP_CONF"
    mv "$TMP_CONF" "$PLUGINS_CONF"
    echo "  Existing entry updated in plugins.conf."
else
    echo "$PLUGIN_ENTRY" >> "$PLUGINS_CONF"
    echo "  New entry appended to plugins.conf."
fi

# ── Step 6: Persist across reboots (optional init.d hook) ────────────────────
echo "[6/7] Checking init.d startup script..."

# Nothing extra needed for GPIO under libgpiod — the kernel exposes gpiochipN
# automatically; no daemon like pigpiod is required.
# We only ensure the OpenPLC init script references the correct working dir.

if [ -f "$INIT_SCRIPT" ]; then
    echo "  Init script found: $INIT_SCRIPT"
    # Verify it cd's into OPENPLC_DIR before exec
    if ! grep -q "cd.*$OPENPLC_DIR" "$INIT_SCRIPT" 2>/dev/null; then
        echo "  WARNING: Init script may not cd to $OPENPLC_DIR."
        echo "  Verify that WorkingDirectory (or cd) is set correctly in $INIT_SCRIPT."
    else
        echo "  Init script working directory OK."
    fi
else
    echo "  No init script found at $INIT_SCRIPT."
    echo "  Creating minimal /etc/init.d/S90openplc ..."

    cat > "$INIT_SCRIPT" <<INITEOF
#!/bin/sh
# OpenPLC Runtime V4 — BusyBox init script
# Generated by rk3506_gpio installer

OPENPLC_DIR="$OPENPLC_DIR"
DAEMON="\$OPENPLC_DIR/openplc_runtime"
PIDFILE="/var/run/openplc.pid"

case "\$1" in
  start)
    echo -n "Starting OpenPLC Runtime: "
    cd "\$OPENPLC_DIR"
    start-stop-daemon -S -b -m -p "\$PIDFILE" -x "\$DAEMON"
    echo "OK"
    ;;
  stop)
    echo -n "Stopping OpenPLC Runtime: "
    start-stop-daemon -K -p "\$PIDFILE"
    echo "OK"
    ;;
  restart)
    "\$0" stop
    sleep 1
    "\$0" start
    ;;
  *)
    echo "Usage: \$0 {start|stop|restart}"
    exit 1
    ;;
esac

exit 0
INITEOF

    chmod 755 "$INIT_SCRIPT"
    echo "  Created $INIT_SCRIPT"
fi

# ── Step 7: Restart OpenPLC and verify ───────────────────────────────────────
echo "[7/7] Restarting OpenPLC Runtime..."

# BusyBox init uses the S/K script convention; no systemctl available
if [ -x "$INIT_SCRIPT" ]; then
    "$INIT_SCRIPT" restart
else
    echo "  WARNING: Cannot restart — $INIT_SCRIPT not executable."
    echo "  Restart OpenPLC manually: $INIT_SCRIPT restart"
    exit 0
fi

# Allow the process a moment to come up
sleep 3

# Check if the runtime process is alive
RUNTIME_BIN=$(basename "$(grep -E "^DAEMON=" "$INIT_SCRIPT" 2>/dev/null \
    | cut -d= -f2 | tr -d '"')" 2>/dev/null || echo "openplc_runtime")

if pgrep -x "$RUNTIME_BIN" >/dev/null 2>&1; then
    echo ""
    echo "============================================"
    echo " Installation complete!"
    echo "============================================"
    echo ""
    echo " GPIO chip : $GPIOCHIP_PATH"
    echo " GPIO mapping active (RK3506, gpiochip1):"
    echo "   Outputs  GPIO1_B3(off=11)=%QX0.0  GPIO1_B4(off=12)=%QX0.1"
    echo "            GPIO1_B5(off=13)=%QX0.2  GPIO1_B6(off=14)=%QX0.3"
    echo "   Inputs   GPIO1_C0(off=16)=%IX0.0  GPIO1_C1(off=17)=%IX0.1"
    echo "            GPIO1_C2(off=18)=%IX0.2  GPIO1_C3(off=19)=%IX0.3"
    echo "            GPIO1_C4(off=20)=%IX0.4  GPIO1_C5(off=21)=%IX0.5"
    echo "            GPIO1_C6(off=22)=%IX0.6  GPIO1_C7(off=23)=%IX0.7"
    echo ""
    echo " To verify GPIO lines on target:"
    echo "   gpioinfo gpiochip1"
    echo "   gpioget  gpiochip1 16    # read GPIO1_C0"
    echo "   gpioset  gpiochip1 11=1  # drive GPIO1_B3 HIGH"
    echo ""
    echo " To monitor OpenPLC log:"
    echo "   tail -f $OPENPLC_DIR/webserver/openplc.log"
    echo ""
else
    echo ""
    echo "ERROR: OpenPLC Runtime did not start."
    echo "  Check the log: tail -f $OPENPLC_DIR/webserver/openplc.log"
    exit 1
fi
