"""Periodic active health exporter for Thread network.

Connects to the OpenThread CLI device, collects topology and health data,
and writes Prometheus metrics to a textfile collector .prom file.
Also discovers device names via DNS-SD and updates the shared devices.json.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from .cli import DEFAULT_TIMEOUT, LONG_TIMEOUT, OTCLIConnection, OTCLIError
from .parsers import parse_table

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_PROM_FILE = "/var/lib/alloy/textfile_collector/thread_active.prom"
DEFAULT_DEVICES_FILE = "/opt/thread-monitor/devices.json"


def collect_topology(conn: OTCLIConnection) -> dict:
    """Collect router, neighbor, and child tables."""
    result = {}

    try:
        result["state"] = conn.send_command("state").strip()
    except (OTCLIError, TimeoutError) as e:
        logger.warning("Failed to get state: %s", e)
        result["state"] = "unknown"

    try:
        result["routers"] = parse_table(conn.send_command("router table"))
    except (OTCLIError, TimeoutError) as e:
        logger.warning("Failed to get router table: %s", e)
        result["routers"] = []

    try:
        result["neighbors"] = parse_table(conn.send_command("neighbor table"))
    except (OTCLIError, TimeoutError) as e:
        logger.warning("Failed to get neighbor table: %s", e)
        result["neighbors"] = []

    try:
        result["children"] = parse_table(conn.send_command("child table"))
    except (OTCLIError, TimeoutError) as e:
        logger.warning("Failed to get child table: %s", e)
        result["children"] = []

    try:
        result["rloc16"] = conn.send_command("rloc16").strip()
    except (OTCLIError, TimeoutError) as e:
        logger.warning("Failed to get rloc16: %s", e)
        result["rloc16"] = ""

    try:
        result["leader_data"] = {}
        for line in conn.send_command("leaderdata").strip().split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                result["leader_data"][k.strip()] = v.strip()
    except (OTCLIError, TimeoutError) as e:
        logger.warning("Failed to get leader data: %s", e)

    return result


def ping_device(conn: OTCLIConnection, addr: str) -> float | None:
    """Ping a device and return RTT in ms, or None if unreachable."""
    try:
        response = conn.send_command(f"ping {addr}", timeout=LONG_TIMEOUT)
        # Parse "16 bytes from <addr>: icmp_seq=1 hlim=64 time=42ms"
        m = re.search(r"time=(\d+)ms", response)
        if m:
            return float(m.group(1))
        # Check for no-reply patterns
        if "no reply" in response.lower() or not response.strip():
            return None
        return None
    except (OTCLIError, TimeoutError):
        return None


def ext_mac_to_link_local(ext_mac: str) -> str:
    """Convert extended MAC (EUI-64) to IPv6 link-local address."""
    mac = ext_mac.lower().replace(":", "").replace("-", "")
    first_byte = int(mac[0:2], 16) ^ 0x02
    b = bytes([first_byte]) + bytes.fromhex(mac[2:])
    return (
        f"fe80::{b[0]:02x}{b[1]:02x}:{b[2]:02x}ff:fe{b[3]:02x}:{b[4]:02x}{b[5]:02x}"
        if len(b) == 6
        else f"fe80::{b[0]:02x}{b[1]:02x}:{b[2]:02x}{b[3]:02x}:{b[4]:02x}{b[5]:02x}:{b[6]:02x}{b[7]:02x}"
    )


def collect_pings(conn: OTCLIConnection, neighbors: list[dict]) -> dict[str, float | None]:
    """Ping all neighbors by their link-local address."""
    results = {}
    for neighbor in neighbors:
        ext_mac = neighbor.get("Extended MAC", "")
        rloc16 = neighbor.get("RLOC16", "")
        if not ext_mac or ext_mac == "0000000000000000":
            continue
        ll_addr = ext_mac_to_link_local(ext_mac)
        rtt = ping_device(conn, ll_addr)
        results[rloc16] = rtt
        logger.info("Ping %s (%s): %s", rloc16, ext_mac,
                     f"{rtt:.0f}ms" if rtt is not None else "timeout")
    return results


def discover_dns_names(conn: OTCLIConnection) -> dict[str, list[str]]:
    """Browse DNS-SD services and return service type -> list of instance names."""
    names = {}
    for svc_type in ["_hap._udp.default.service.arpa", "_meshcop._udp.default.service.arpa"]:
        try:
            response = conn.send_command(f"dns browse {svc_type}", timeout=LONG_TIMEOUT)
            instances = []
            for line in response.strip().split("\n"):
                line = line.strip()
                if line.startswith("DNS browse") or not line:
                    continue
                instances.append(line)
            if instances:
                names[svc_type] = instances
                logger.info("DNS-SD %s: %s", svc_type, instances)
        except (OTCLIError, TimeoutError) as e:
            logger.warning("DNS browse %s failed: %s", svc_type, e)
    return names


def resolve_meshcop_eui64s(conn: OTCLIConnection,
                           names: list[str]) -> dict[str, str]:
    """Resolve meshcop border routers to EUI-64 via the xa TXT field.

    Returns {eui64_key: room_name} e.g. {"0x522B784FF40A4D82": "Family Room"}.
    """
    result = {}
    for name in names:
        try:
            escaped = name.replace(" ", "\\ ")
            response = conn.send_command(
                f"dns service {escaped} _meshcop._udp.default.service.arpa.",
                timeout=LONG_TIMEOUT,
            )
            # Parse xa= from TXT record: xa=522b784ff40a4d82
            m = re.search(r"\bxa=([0-9a-fA-F]{16})\b", response)
            if m:
                eui_key = f"0x{m.group(1).upper()}"
                result[eui_key] = name
                logger.info("Resolved border router '%s' -> %s", name, eui_key)
            else:
                logger.debug("No xa field in meshcop TXT for '%s'", name)
        except (OTCLIError, TimeoutError) as e:
            logger.warning("dns service '%s' failed: %s", name, e)
    return result


def resolve_hap_to_rloc(conn: OTCLIConnection,
                        hap_names: list[str]) -> dict[str, str]:
    """Resolve HAP devices to RLOC16 via DNS hostname → ML-EID → ping → eidcache.

    Returns {rloc16: hap_name} e.g. {"0xD000": "Eve Energy C8B7"}.
    """
    # Step 1: Resolve hostnames to ML-EID addresses
    ml_eids = {}  # ml_eid -> hap_name
    for name in hap_names:
        try:
            hostname = name.replace(" ", "-") + ".default.service.arpa."
            escaped_hostname = hostname.replace(" ", "\\ ")
            response = conn.send_command(
                f"dns resolve {escaped_hostname}", timeout=LONG_TIMEOUT
            )
            # Parse "DNS response for ... - <addr> TTL:300"
            m = re.search(r"-\s+(fd[0-9a-f:]+)\s+TTL", response)
            if m:
                ml_eids[m.group(1)] = name
        except (OTCLIError, TimeoutError) as e:
            logger.debug("dns resolve '%s' failed: %s", name, e)

    if not ml_eids:
        return {}

    logger.info("Resolved %d HAP hostnames to ML-EIDs", len(ml_eids))

    # Step 2: Ping each to populate the EID cache
    for addr in ml_eids:
        try:
            conn.send_command(f"ping {addr} 1 1 1", timeout=LONG_TIMEOUT)
        except (OTCLIError, TimeoutError):
            pass

    # Step 3: Read EID cache to get RLOC16 mappings
    result = {}
    try:
        cache = conn.send_command("eidcache")
        for line in cache.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 2:
                eid_addr = parts[0]
                rloc_hex = parts[1]
                if eid_addr in ml_eids:
                    rloc16 = _normalize_rloc16(f"0x{rloc_hex}")
                    result[rloc16] = ml_eids[eid_addr]
                    logger.info("HAP '%s' -> RLOC %s", ml_eids[eid_addr], rloc16)
    except (OTCLIError, TimeoutError) as e:
        logger.warning("eidcache failed: %s", e)

    return result


def load_devices(path: str) -> dict:
    """Load devices.json."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_devices(path: str, devices: dict) -> None:
    """Atomically save devices.json."""
    tmp = Path(path).with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(devices, f, indent=2)
            f.write("\n")
        tmp.rename(path)
    except OSError as e:
        logger.warning("Failed to save devices: %s", e)


def _normalize_rloc16(rloc: str) -> str:
    """Normalize RLOC16 to uppercase 0xNNNN format for consistent lookups."""
    if rloc.startswith("0x") or rloc.startswith("0X"):
        try:
            return f"0x{int(rloc, 16):04X}"
        except ValueError:
            pass
    return rloc


def update_devices_from_topology(devices: dict, routers: list[dict],
                                 children: list[dict]) -> bool:
    """Add any new EUI-64 <-> RLOC16 mappings. Returns True if changed."""
    changed = False
    for entry in routers + children:
        ext_mac = entry.get("Extended MAC", "")
        rloc16 = _normalize_rloc16(entry.get("RLOC16", ""))
        if not ext_mac or ext_mac == "0000000000000000" or not rloc16:
            continue
        eui_key = f"0x{ext_mac.upper()}"
        # If we have a name for the RLOC but not the EUI-64, add the EUI mapping
        if rloc16 in devices and eui_key not in devices:
            devices[eui_key] = devices[rloc16]
            changed = True
            logger.info("Added EUI-64 mapping: %s -> %s", eui_key, devices[rloc16])
        # If we have a name for the EUI but not the RLOC, add the RLOC mapping
        elif eui_key in devices and rloc16 not in devices:
            devices[rloc16] = devices[eui_key]
            changed = True
            logger.info("Added RLOC16 mapping: %s -> %s", rloc16, devices[eui_key])
    return changed


def update_devices_from_dns(devices: dict, dns_names: dict,
                            meshcop_eui64s: dict[str, str] | None = None,
                            hap_rlocs: dict[str, str] | None = None) -> bool:
    """Store discovered DNS-SD names and rename border routers. Returns True if changed."""
    changed = False
    # Store border router names
    meshcop = dns_names.get("_meshcop._udp.default.service.arpa", [])
    if meshcop:
        key = "_meshcop_names"
        old = devices.get(key)
        if old != meshcop:
            devices[key] = meshcop
            changed = True
    # Store HAP device names
    hap = dns_names.get("_hap._udp.default.service.arpa", [])
    if hap:
        key = "_hap_names"
        old = devices.get(key)
        if old != hap:
            devices[key] = hap
            changed = True
    # Rename border routers using resolved EUI-64 -> room name
    if meshcop_eui64s:
        for eui_key, room_name in meshcop_eui64s.items():
            if eui_key in devices and devices[eui_key] != room_name:
                old_name = devices[eui_key]
                devices[eui_key] = room_name
                # Also update any RLOC16 entries that had the old name
                for addr, name in list(devices.items()):
                    if name == old_name and addr != eui_key and isinstance(name, str):
                        devices[addr] = room_name
                        logger.info("Renamed %s: '%s' -> '%s'", addr, old_name, room_name)
                logger.info("Renamed %s: '%s' -> '%s'", eui_key, old_name, room_name)
                changed = True
            elif eui_key not in devices:
                devices[eui_key] = room_name
                logger.info("Added border router %s -> '%s'", eui_key, room_name)
                changed = True
    # Store HAP device -> device name correlations
    if hap_rlocs:
        hap_map = {}
        for rloc16, hap_name in hap_rlocs.items():
            device_name = _device_name(devices, rloc16)
            if device_name:
                hap_map[hap_name] = device_name
        key = "_hap_device_map"
        old = devices.get(key)
        if old != hap_map and hap_map:
            devices[key] = hap_map
            changed = True
            for hap_name, dev_name in hap_map.items():
                logger.info("HAP correlation: '%s' = '%s'", hap_name, dev_name)
    return changed


def _device_name(devices: dict, *keys: str) -> str:
    """Look up device name, trying each key with normalized RLOC16 case."""
    for key in keys:
        if key in devices:
            return devices[key]
        normalized = _normalize_rloc16(key)
        if normalized in devices:
            return devices[normalized]
        # Also try uppercase EUI format
        if key.startswith("0x") and len(key) > 6:
            upper_key = f"0x{key[2:].upper()}"
            if upper_key in devices:
                return devices[upper_key]
    return ""


def write_metrics(prom_path: str, topology: dict, pings: dict[str, float | None],
                  dns_names: dict, devices: dict) -> None:
    """Write Prometheus metrics to textfile collector."""
    lines = []

    # Probe device state
    state = topology.get("state", "unknown")
    rloc16 = topology.get("rloc16", "")
    lines.append("# HELP thread_active_probe_info Active probe device info")
    lines.append("# TYPE thread_active_probe_info gauge")
    lines.append(f'thread_active_probe_info{{state="{state}",rloc16="{rloc16}"}} 1')
    lines.append("")

    # Router count
    routers = topology.get("routers", [])
    lines.append("# HELP thread_active_router_count Number of routers in the mesh")
    lines.append("# TYPE thread_active_router_count gauge")
    lines.append(f"thread_active_router_count {len(routers)}")
    lines.append("")

    # Neighbor count (direct links)
    neighbors = topology.get("neighbors", [])
    lines.append("# HELP thread_active_neighbor_count Direct neighbors of probe device")
    lines.append("# TYPE thread_active_neighbor_count gauge")
    lines.append(f"thread_active_neighbor_count {len(neighbors)}")
    lines.append("")

    # Child count
    children = topology.get("children", [])
    lines.append("# HELP thread_active_child_count Children attached to probe device")
    lines.append("# TYPE thread_active_child_count gauge")
    lines.append(f"thread_active_child_count {len(children)}")
    lines.append("")

    # Leader info
    leader = topology.get("leader_data", {})
    if leader:
        # Leader Router ID needs conversion to RLOC16: router_id << 10
        leader_router_id = leader.get("Leader Router ID", "")
        leader_rloc = ""
        if leader_router_id:
            try:
                leader_rloc = f"0x{int(leader_router_id) << 10:04X}"
            except ValueError:
                pass
        partition = leader.get("Partition ID", "")
        weight = leader.get("Weighting", "")
        leader_name = _device_name(devices, leader_rloc)
        lines.append("# HELP thread_active_leader_info Current leader from active probe")
        lines.append("# TYPE thread_active_leader_info gauge")
        label_parts = [f'leader_rloc16="{leader_rloc}"']
        if leader_name:
            label_parts.append(f'leader_device="{leader_name}"')
        if partition:
            label_parts.append(f'partition_id="{partition}"')
        if weight:
            label_parts.append(f'weighting="{weight}"')
        lines.append(f'thread_active_leader_info{{{",".join(label_parts)}}} 1')
        lines.append("")

    # Per-neighbor RSSI from mesh perspective
    if neighbors:
        lines.append("# HELP thread_active_neighbor_rssi Neighbor RSSI from mesh (dBm)")
        lines.append("# TYPE thread_active_neighbor_rssi gauge")
        for n in neighbors:
            rloc = n.get("RLOC16", "")
            ext_mac = n.get("Extended MAC", "")
            avg_rssi = n.get("Avg RSSI", "")
            name = _device_name(devices, rloc, f"0x{ext_mac.upper()}")
            label_parts = [f'rloc16="{rloc}"']
            if ext_mac:
                label_parts.append(f'ext_mac="{ext_mac}"')
            if name:
                label_parts.append(f'device="{name}"')
            if avg_rssi:
                lines.append(f'thread_active_neighbor_rssi{{{",".join(label_parts)}}} {avg_rssi}')
        lines.append("")

        lines.append("# HELP thread_active_neighbor_last_rssi Last RSSI from neighbor (dBm)")
        lines.append("# TYPE thread_active_neighbor_last_rssi gauge")
        for n in neighbors:
            rloc = n.get("RLOC16", "")
            last_rssi = n.get("Last RSSI", "")
            name = _device_name(devices, rloc)
            label_parts = [f'rloc16="{rloc}"']
            if name:
                label_parts.append(f'device="{name}"')
            if last_rssi:
                lines.append(f'thread_active_neighbor_last_rssi{{{",".join(label_parts)}}} {last_rssi}')
        lines.append("")

    # Per-router link quality
    if routers:
        lines.append("# HELP thread_active_router_link_quality Router link quality (LQ In)")
        lines.append("# TYPE thread_active_router_link_quality gauge")
        for r in routers:
            rloc = r.get("RLOC16", "")
            ext_mac = r.get("Extended MAC", "")
            lq_in = r.get("LQ In", "")
            path_cost = r.get("Path Cost", "")
            name = _device_name(devices, rloc, f"0x{ext_mac.upper()}")
            label_parts = [f'rloc16="{rloc}"']
            if ext_mac and ext_mac != "0000000000000000":
                label_parts.append(f'ext_mac="{ext_mac}"')
            if name:
                label_parts.append(f'device="{name}"')
            if lq_in:
                lines.append(f'thread_active_router_link_quality{{{",".join(label_parts)}}} {lq_in}')
        lines.append("")

        lines.append("# HELP thread_active_router_path_cost Router path cost")
        lines.append("# TYPE thread_active_router_path_cost gauge")
        for r in routers:
            rloc = r.get("RLOC16", "")
            path_cost = r.get("Path Cost", "")
            name = _device_name(devices, rloc)
            label_parts = [f'rloc16="{rloc}"']
            if name:
                label_parts.append(f'device="{name}"')
            if path_cost:
                lines.append(f'thread_active_router_path_cost{{{",".join(label_parts)}}} {path_cost}')
        lines.append("")

    # Ping results
    if pings:
        lines.append("# HELP thread_active_ping_rtt Ping RTT to neighbor (ms, -1 = timeout)")
        lines.append("# TYPE thread_active_ping_rtt gauge")
        for rloc, rtt in sorted(pings.items()):
            name = _device_name(devices, rloc)
            label_parts = [f'rloc16="{rloc}"']
            if name:
                label_parts.append(f'device="{name}"')
            value = f"{rtt:.0f}" if rtt is not None else "-1"
            lines.append(f'thread_active_ping_rtt{{{",".join(label_parts)}}} {value}')
        lines.append("")

        lines.append("# HELP thread_active_ping_reachable Neighbor reachable via ping (1=yes, 0=no)")
        lines.append("# TYPE thread_active_ping_reachable gauge")
        for rloc, rtt in sorted(pings.items()):
            name = _device_name(devices, rloc)
            label_parts = [f'rloc16="{rloc}"']
            if name:
                label_parts.append(f'device="{name}"')
            lines.append(f'thread_active_ping_reachable{{{",".join(label_parts)}}} {1 if rtt is not None else 0}')
        lines.append("")

    # DNS-SD discovered services
    hap_names = dns_names.get("_hap._udp.default.service.arpa", [])
    meshcop_names = dns_names.get("_meshcop._udp.default.service.arpa", [])
    lines.append("# HELP thread_active_hap_devices HomeKit accessory count from DNS-SD")
    lines.append("# TYPE thread_active_hap_devices gauge")
    lines.append(f"thread_active_hap_devices {len(hap_names)}")
    lines.append("")
    lines.append("# HELP thread_active_border_routers Border router count from DNS-SD")
    lines.append("# TYPE thread_active_border_routers gauge")
    lines.append(f"thread_active_border_routers {len(meshcop_names)}")
    lines.append("")

    # Individual service names as info metrics
    if hap_names:
        lines.append("# HELP thread_active_hap_device_info HomeKit device discovered via DNS-SD")
        lines.append("# TYPE thread_active_hap_device_info gauge")
        for name in hap_names:
            lines.append(f'thread_active_hap_device_info{{name="{name}"}} 1')
        lines.append("")

    if meshcop_names:
        lines.append("# HELP thread_active_border_router_info Border router discovered via DNS-SD")
        lines.append("# TYPE thread_active_border_router_info gauge")
        for name in meshcop_names:
            lines.append(f'thread_active_border_router_info{{name="{name}"}} 1')
        lines.append("")

    # Collection timestamp
    lines.append("# HELP thread_active_last_collection_timestamp Unix time of last collection")
    lines.append("# TYPE thread_active_last_collection_timestamp gauge")
    lines.append(f"thread_active_last_collection_timestamp {time.time():.0f}")
    lines.append("")

    content = "\n".join(lines) + "\n"

    # Atomic write
    tmp = Path(prom_path).with_suffix(".tmp")
    try:
        Path(prom_path).parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content)
        tmp.rename(prom_path)
        logger.info("Wrote %d metrics lines to %s", len(lines), prom_path)
    except OSError as e:
        logger.warning("Failed to write metrics: %s", e)


def run_once(port: str, baudrate: int, prom_path: str, devices_path: str) -> None:
    """Run a single collection cycle."""
    conn = OTCLIConnection(port, baudrate)
    try:
        conn.open()

        # Load device names
        devices = load_devices(devices_path)

        # Collect topology
        logger.info("Collecting topology...")
        topology = collect_topology(conn)
        logger.info("State: %s, Routers: %d, Neighbors: %d, Children: %d",
                     topology["state"], len(topology["routers"]),
                     len(topology["neighbors"]), len(topology["children"]))

        # Update devices from topology (new EUI-64/RLOC16 mappings)
        devices_changed = update_devices_from_topology(
            devices, topology["routers"], topology["children"]
        )

        # Ping neighbors
        logger.info("Pinging %d neighbors...", len(topology["neighbors"]))
        pings = collect_pings(conn, topology["neighbors"])

        # Discover DNS-SD names and resolve border router EUI-64s
        logger.info("Discovering DNS-SD names...")
        dns_names = discover_dns_names(conn)
        meshcop_eui64s = {}
        meshcop_names = dns_names.get("_meshcop._udp.default.service.arpa", [])
        if meshcop_names:
            logger.info("Resolving %d border router EUI-64s...", len(meshcop_names))
            meshcop_eui64s = resolve_meshcop_eui64s(conn, meshcop_names)
        # Resolve HAP devices to RLOC16 via EID cache
        hap_rlocs = {}
        hap_names = dns_names.get("_hap._udp.default.service.arpa", [])
        if hap_names:
            logger.info("Resolving %d HAP devices to RLOC16...", len(hap_names))
            hap_rlocs = resolve_hap_to_rloc(conn, hap_names)
        if update_devices_from_dns(devices, dns_names, meshcop_eui64s, hap_rlocs):
            devices_changed = True

        # Save updated devices if changed
        if devices_changed:
            save_devices(devices_path, devices)
            logger.info("Updated %s", devices_path)

        # Write metrics
        write_metrics(prom_path, topology, pings, dns_names, devices)

    except Exception as e:
        logger.error("Collection failed: %s", e)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Thread active health exporter")
    parser.add_argument("--port", default=os.environ.get("OT_CLI_PORT", "/dev/ttyACM1"),
                        help="Serial port (env: OT_CLI_PORT)")
    parser.add_argument("--baudrate", type=int,
                        default=int(os.environ.get("OT_CLI_BAUDRATE", "115200")),
                        help="Baud rate (env: OT_CLI_BAUDRATE)")
    parser.add_argument("--prom-file", default=os.environ.get("THREAD_ACTIVE_PROM_FILE", DEFAULT_PROM_FILE),
                        help="Prometheus textfile path")
    parser.add_argument("--devices-file", default=os.environ.get("THREAD_DEVICES_FILE", DEFAULT_DEVICES_FILE),
                        help="Path to devices.json")
    parser.add_argument("--log-level", default=os.environ.get("OT_CLI_LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    logger.info("Thread active exporter starting — port %s", args.port)
    run_once(args.port, args.baudrate, args.prom_file, args.devices_file)
    logger.info("Done")


if __name__ == "__main__":
    main()
