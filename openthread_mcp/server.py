"""OpenThread CLI MCP Server.

Exposes the OpenThread CLI on an Arduino Nano Matter as MCP tools
for network monitoring, investigation, and repair.
"""

import argparse
import json
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from .cli import DEFAULT_TIMEOUT, LONG_TIMEOUT, OTCLIConnection, OTCLIError
from .parsers import (
    parse_counters,
    parse_dataset,
    parse_diagnostic,
    parse_ipaddrs,
    parse_key_value,
    parse_network_data,
    parse_scan,
    parse_table,
)

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "openthread",
    instructions=(
        "OpenThread CLI tools for monitoring and managing a Thread network "
        "via an Arduino Nano Matter. Use get_network_status for a quick "
        "overview, get_topology for mesh structure, and get_diagnostics to "
        "investigate specific devices. The send_command tool provides raw "
        "CLI access for anything not covered by dedicated tools."
    ),
)

# Global connection — initialised at startup
_conn: OTCLIConnection | None = None


def _get_conn() -> OTCLIConnection:
    global _conn
    if _conn is None:
        raise RuntimeError("Serial connection not initialised")
    if not _conn.is_open:
        _conn.open()
    return _conn


# ---------------------------------------------------------------------------
# Network status
# ---------------------------------------------------------------------------


@mcp.tool()
def get_network_status() -> str:
    """Get a summary of the device's Thread network status.

    Returns state, role, RLOC16, network name, channel, PAN ID,
    partition ID, leader info, TX power, and uptime counters.
    """
    conn = _get_conn()
    fields = {}
    for cmd in [
        "state",
        "rloc16",
        "networkname",
        "channel",
        "panid",
        "extpanid",
        "leaderdata",
        "txpower",
        "eui64",
        "extaddr",
        "version",
    ]:
        try:
            fields[cmd] = conn.send_command(cmd)
        except (OTCLIError, TimeoutError) as e:
            fields[cmd] = f"error: {e}"

    return json.dumps(fields, indent=2)


@mcp.tool()
def get_dataset() -> str:
    """Get the active operational dataset (network credentials and parameters)."""
    conn = _get_conn()
    raw = conn.send_command("dataset active")
    parsed = parse_dataset(raw)
    return json.dumps(parsed, indent=2)


@mcp.tool()
def get_ipaddresses() -> str:
    """Get all IPv6 addresses assigned to this device."""
    conn = _get_conn()
    raw = conn.send_command("ipaddr")
    addrs = parse_ipaddrs(raw)
    return json.dumps(addrs, indent=2)


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


@mcp.tool()
def get_topology() -> str:
    """Get the full mesh topology: router table, neighbor table, and child table.

    The router table shows all routers in the network with link quality
    and path costs. The neighbor table shows direct radio neighbors with
    RSSI measurements. The child table shows devices attached to us
    (only populated when we are a router or leader).
    """
    conn = _get_conn()
    result = {}

    for name, cmd in [
        ("router_table", "router table"),
        ("neighbor_table", "neighbor table"),
        ("child_table", "child table"),
    ]:
        try:
            raw = conn.send_command(cmd)
            result[name] = parse_table(raw)
        except (OTCLIError, TimeoutError) as e:
            result[name] = f"error: {e}"

    return json.dumps(result, indent=2)


@mcp.tool()
def get_neighbor_table() -> str:
    """Get direct radio neighbors with RSSI, link quality, and version info."""
    conn = _get_conn()
    raw = conn.send_command("neighbor table")
    return json.dumps(parse_table(raw), indent=2)


@mcp.tool()
def get_router_table() -> str:
    """Get all routers in the mesh with link quality, path cost, and extended MAC."""
    conn = _get_conn()
    raw = conn.send_command("router table")
    return json.dumps(parse_table(raw), indent=2)


@mcp.tool()
def get_child_table() -> str:
    """Get children attached to this router (empty if we are a child)."""
    conn = _get_conn()
    raw = conn.send_command("child table")
    return json.dumps(parse_table(raw), indent=2)


# ---------------------------------------------------------------------------
# Network data
# ---------------------------------------------------------------------------


@mcp.tool()
def get_network_data() -> str:
    """Get Thread network data: prefixes, routes, services, and commissioning info.

    Shows which border routers advertise off-mesh routes, OMR prefixes,
    SRP services, and DNS services.
    """
    conn = _get_conn()
    raw = conn.send_command("netdata show")
    parsed = parse_network_data(raw)
    return json.dumps(parsed, indent=2)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@mcp.tool()
def get_counters() -> str:
    """Get MAC and MLE counters for this device.

    MAC counters show TX/RX frame statistics, retries, and errors.
    MLE counters show role changes, attach attempts, and time in each role.
    """
    conn = _get_conn()
    result = {}

    for name, cmd in [("mac", "counters mac"), ("mle", "counters mle")]:
        try:
            raw = conn.send_command(cmd)
            result[name] = parse_counters(raw)
        except (OTCLIError, TimeoutError) as e:
            result[name] = f"error: {e}"

    return json.dumps(result, indent=2)


@mcp.tool()
def reset_counters() -> str:
    """Reset MAC and MLE counters to zero."""
    conn = _get_conn()
    conn.send_command("counters mac reset")
    conn.send_command("counters mle reset")
    return "Counters reset."


@mcp.tool()
def get_device_diagnostics(rloc16: str) -> str:
    """Query a remote Thread device for its diagnostic information.

    Uses Thread Network Diagnostics to request extended MAC, RLOC16,
    mode flags, and MAC counters from the specified device.

    Args:
        rloc16: The RLOC16 of the target device (e.g. "0x4000").
    """
    conn = _get_conn()
    # Build mesh-local RLOC address from the RLOC16
    # Get our mesh-local prefix first
    prefix_raw = conn.send_command("dataset active")
    prefix_data = parse_dataset(prefix_raw)
    mesh_prefix = prefix_data.get("Mesh Local Prefix", "")

    if not mesh_prefix:
        return json.dumps({"error": "Could not determine mesh-local prefix"})

    # Construct RLOC address: <prefix>::ff:fe00:<rloc16>
    prefix_base = mesh_prefix.replace("/64", "").rstrip(":")
    rloc_hex = rloc16.lower().replace("0x", "")
    addr = f"{prefix_base}:0:ff:fe00:{rloc_hex}"

    # TLV types: 0=ExtMac, 1=Rloc16, 2=Mode, 9=MacCounters, 14=ChildTable
    try:
        raw = conn.send_command(
            f"networkdiagnostic get {addr} 0 1 2 9 14",
            timeout=LONG_TIMEOUT,
        )
        parsed = parse_diagnostic(raw)
        return json.dumps(parsed, indent=2)
    except OTCLIError as e:
        return json.dumps({"error": str(e), "address": addr})
    except TimeoutError:
        return json.dumps(
            {"error": "Device did not respond to diagnostics", "address": addr}
        )


# ---------------------------------------------------------------------------
# Active probing
# ---------------------------------------------------------------------------


@mcp.tool()
def ping(address: str, count: int = 1, size: int = 16) -> str:
    """Ping a Thread device by IPv6 address or RLOC16.

    If a 4-character hex value is provided (e.g. "d000"), it is treated
    as an RLOC16 and converted to the mesh-local RLOC address.

    Args:
        address: IPv6 address or RLOC16 (e.g. "d000", "0x4000", or full IPv6).
        count: Number of pings to send (default 1).
        size: Payload size in bytes (default 16).
    """
    conn = _get_conn()

    # If it looks like a bare RLOC16, convert to mesh-local address
    addr = address.strip().lower().replace("0x", "")
    if len(addr) <= 4 and all(c in "0123456789abcdef" for c in addr):
        prefix_raw = conn.send_command("dataset active")
        prefix_data = parse_dataset(prefix_raw)
        mesh_prefix = prefix_data.get("Mesh Local Prefix", "").replace("/64", "").rstrip(":")
        addr = f"{mesh_prefix}:0:ff:fe00:{addr}"
    else:
        addr = address.strip()

    timeout = LONG_TIMEOUT + (count * 3)
    raw = conn.send_command(f"ping {addr} {size} {count}", timeout=timeout)
    return raw


@mcp.tool()
def scan() -> str:
    """Scan for nearby 802.15.4 networks and devices.

    Performs an active scan across all channels. Returns a table of
    detected PANs with MAC addresses, channels, and signal strength.
    Note: This briefly takes the device off-network.
    """
    conn = _get_conn()
    raw = conn.send_command("scan", timeout=LONG_TIMEOUT)
    return json.dumps(parse_scan(raw), indent=2)


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


@mcp.tool()
def thread_start() -> str:
    """Start the Thread protocol. The device will join the network using
    the committed dataset. Use after thread_stop or after a factory reset
    with new credentials."""
    conn = _get_conn()
    conn.send_command("ifconfig up")
    conn.send_command("thread start")
    return "Thread interface up and started."


@mcp.tool()
def thread_stop() -> str:
    """Stop the Thread protocol and detach from the network."""
    conn = _get_conn()
    conn.send_command("thread stop")
    conn.send_command("ifconfig down")
    return "Thread stopped and interface down."


@mcp.tool()
def get_preferred_role() -> str:
    """Get the device's current Thread role preference and weight.

    Returns the current state, router eligibility mode, weight,
    and upgrade/downgrade thresholds.
    """
    conn = _get_conn()
    fields = {}
    for cmd in [
        "state",
        "mode",
        "leaderweight",
        "routerupgradethreshold",
        "routerdowngradethreshold",
        "partitionid",
    ]:
        try:
            fields[cmd] = conn.send_command(cmd)
        except (OTCLIError, TimeoutError) as e:
            fields[cmd] = f"error: {e}"

    return json.dumps(fields, indent=2)


@mcp.tool()
def set_preferred_role(role: str) -> str:
    """Change this device's Thread role.

    Can promote the device to router, demote to child, or force leader
    election. The network will rebalance automatically.

    Args:
        role: Target role — "router", "child", or "leader".
              "router" requests promotion (may take a few seconds).
              "child" forces demotion from router.
              "leader" forces a leader election (use with caution).
    """
    conn = _get_conn()
    role = role.strip().lower()
    if role not in ("router", "child", "leader"):
        return f"Invalid role '{role}'. Must be 'router', 'child', or 'leader'."
    conn.send_command(f"state {role}")
    return f"Role change to '{role}' requested."


@mcp.tool()
def set_leader_weight(weight: int) -> str:
    """Set this device's leader weight (0-255).

    Higher weight makes the device more likely to become leader during
    a partition merge or leader election. Default is 64. Set to 0 to
    make the device never become leader.

    Changes take effect at the next leader election. To trigger one
    immediately, use set_preferred_role with role="leader".

    Args:
        weight: Leader weight (0-255). Higher = more likely to lead.
    """
    conn = _get_conn()
    if not 0 <= weight <= 255:
        return "Weight must be 0-255."
    conn.send_command(f"leaderweight {weight}")
    return f"Leader weight set to {weight}."


@mcp.tool()
def set_router_thresholds(
    upgrade: int | None = None,
    downgrade: int | None = None,
) -> str:
    """Set the router upgrade and downgrade thresholds.

    These control when the device promotes from child to router
    (upgrade) and when it demotes from router to child (downgrade).
    The thresholds represent the number of routers in the network.

    Args:
        upgrade: Promote to router when fewer than this many routers
                 exist (default 16). Set lower to reduce router count.
        downgrade: Demote to child when more than this many routers
                   exist (default 23). Set higher to keep more routers.
    """
    conn = _get_conn()
    results = []
    if upgrade is not None:
        conn.send_command(f"routerupgradethreshold {upgrade}")
        results.append(f"upgrade={upgrade}")
    if downgrade is not None:
        conn.send_command(f"routerdowngradethreshold {downgrade}")
        results.append(f"downgrade={downgrade}")
    if not results:
        return "No thresholds changed. Provide upgrade and/or downgrade."
    return f"Router thresholds set: {', '.join(results)}."


@mcp.tool()
def set_mode(rx_on_idle: bool = True, ftd: bool = True, network_data: bool = True) -> str:
    """Set the device's MLE mode flags.

    Controls how the device participates in the Thread network.

    Args:
        rx_on_idle: Keep radio on when idle (True for routers, False saves power).
        ftd: Full Thread Device (True) vs Minimal Thread Device (False).
        network_data: Request full network data (True) vs stable-only (False).
    """
    conn = _get_conn()
    mode = ""
    if rx_on_idle:
        mode += "r"
    if ftd:
        mode += "d"
    if network_data:
        mode += "n"
    if not mode:
        mode = "-"
    conn.send_command(f"mode {mode}")
    return f"Mode set to '{mode}'."


@mcp.tool()
def device_reset() -> str:
    """Soft-reset the Thread device. The dataset and network credentials
    persist across resets. The device will rejoin the network automatically
    if Thread was running."""
    conn = _get_conn()
    try:
        conn.send_command("reset", timeout=2.0)
    except TimeoutError:
        pass  # reset doesn't send 'Done'
    # Reopen connection after reset
    import time
    time.sleep(3)
    conn.close()
    conn.open()
    return "Device reset. Reconnected."


@mcp.tool()
def factory_reset() -> str:
    """Factory-reset the device, erasing all stored data including the
    network dataset. The device will need to be reconfigured and rejoined
    to the network. Use with caution."""
    conn = _get_conn()
    try:
        conn.send_command("factoryreset", timeout=2.0)
    except TimeoutError:
        pass
    import time
    time.sleep(3)
    conn.close()
    conn.open()
    return "Factory reset complete. Device is unconfigured."


@mcp.tool()
def set_dataset_and_join(
    networkkey: str,
    channel: int,
    panid: str,
) -> str:
    """Configure the Thread dataset and join the network.

    Sets the network key, channel, and PAN ID, commits the dataset,
    and starts Thread. Use after a factory reset to rejoin.

    Args:
        networkkey: 32-character hex network key.
        channel: Thread channel number (11-26).
        panid: PAN ID as hex string (e.g. "0x00fb").
    """
    conn = _get_conn()
    conn.send_command("dataset clear")
    conn.send_command(f"dataset networkkey {networkkey}")
    conn.send_command(f"dataset channel {channel}")
    conn.send_command(f"dataset panid {panid}")
    conn.send_command("dataset commit active")
    conn.send_command("ifconfig up")
    conn.send_command("thread start")
    return f"Dataset configured and Thread started on channel {channel}."


# ---------------------------------------------------------------------------
# Raw access
# ---------------------------------------------------------------------------


@mcp.tool()
def send_command(command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Send a raw OpenThread CLI command and return the response.

    Use this for commands not covered by dedicated tools. See the full
    command reference at https://openthread.io/reference/cli/commands

    Args:
        command: The OT CLI command to send (e.g. "router table").
        timeout: Max seconds to wait for a response (default 5).
    """
    conn = _get_conn()
    try:
        return conn.send_command(command, timeout=timeout)
    except OTCLIError as e:
        return f"Error: {e}"
    except TimeoutError as e:
        return f"Timeout: {e}"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="OpenThread CLI MCP Server")
    parser.add_argument(
        "--port",
        default=os.environ.get("OT_CLI_PORT", "/dev/cu.usbmodem442600EF3"),
        help="Serial port for the OpenThread CLI device (env: OT_CLI_PORT)",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=int(os.environ.get("OT_CLI_BAUDRATE", "115200")),
        help="Serial baud rate (env: OT_CLI_BAUDRATE)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("OT_CLI_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (env: OT_CLI_LOG_LEVEL)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    global _conn
    _conn = OTCLIConnection(args.port, args.baudrate)
    logger.info("Starting OpenThread MCP server (port=%s)", args.port)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
