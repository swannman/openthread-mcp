# openthread-mcp

MCP server and Prometheus exporter for monitoring and managing a Thread network via the OpenThread CLI on an Arduino Nano Matter.

Two entry points share the same serial transport and CLI parser:

- **`openthread-mcp`** — MCP server exposing Thread CLI tools to Claude Code
- **`openthread-exporter`** — Periodic health exporter that writes Prometheus metrics to a textfile collector

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## MCP Server

### As a standalone server (stdio transport)

```bash
openthread-mcp --port /dev/ttyACM0
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OT_CLI_PORT` | `/dev/cu.usbmodem442600EF3` | Serial port |
| `OT_CLI_BAUDRATE` | `115200` | Baud rate |
| `OT_CLI_LOG_LEVEL` | `INFO` | Log level |

### Claude Code MCP config

```json
{
  "mcpServers": {
    "openthread": {
      "command": "/path/to/openthread-mcp/.venv/bin/openthread-mcp",
      "args": ["--port", "/dev/ttyACM0"],
      "env": {
        "OT_CLI_LOG_LEVEL": "WARNING"
      }
    }
  }
}
```

## Prometheus Exporter

The exporter runs a single collection cycle then exits — designed to be triggered by a systemd timer.

```bash
openthread-exporter --port /dev/ttyACM1 \
  --prom-file /var/lib/alloy/textfile_collector/thread_active.prom \
  --devices-file /opt/thread-monitor/devices.json
```

Each cycle:

1. **Topology** — collects router, neighbor, and child tables
2. **Pings** — ICMPv6 ping to each neighbor via link-local address
3. **DNS-SD discovery** — browses `_hap._udp` and `_meshcop._udp` services
4. **Border router resolution** — extracts EUI-64 from meshcop TXT `xa` field, maps room names to devices
5. **HAP device correlation** — resolves HAP hostnames → ML-EID → ping → eidcache → RLOC16, correlating manufacturer names (e.g. "Eve Energy C8B7") with user-assigned names (e.g. "Deck Lights")
6. **Device map update** — merges new discoveries into `devices.json` (shared with the passive sniffer), preserving entries from previous cycles for sleepy devices

### Metrics

| Metric | Description |
|--------|-------------|
| `thread_active_probe_info` | Probe device state and RLOC16 |
| `thread_active_router_count` | Number of routers in the mesh |
| `thread_active_neighbor_count` | Direct neighbors of the probe |
| `thread_active_child_count` | Children attached to the probe |
| `thread_active_leader_info` | Current leader with RLOC16, device name, partition ID |
| `thread_active_neighbor_rssi` | Neighbor RSSI from mesh (dBm) |
| `thread_active_neighbor_last_rssi` | Last RSSI from neighbor (dBm) |
| `thread_active_router_link_quality` | Router link quality (0–3) |
| `thread_active_router_path_cost` | Router path cost |
| `thread_active_ping_rtt` | Ping RTT (ms, -1 = timeout) |
| `thread_active_ping_reachable` | Neighbor reachable (1/0) |
| `thread_active_hap_devices` | HomeKit accessory count |
| `thread_active_border_routers` | Border router count |
| `thread_active_hap_device_info` | Per-device HAP info label |
| `thread_active_border_router_info` | Per-device border router info label |

### systemd timer

```ini
# /etc/systemd/system/thread-active-exporter.service
[Unit]
Description=Thread active health exporter
After=network.target

[Service]
Type=oneshot
ExecStart=/opt/openthread-mcp/.venv/bin/openthread-exporter \
  --port /dev/ttyACM1 \
  --prom-file /var/lib/alloy/textfile_collector/thread_active.prom \
  --devices-file /opt/thread-monitor/devices.json
User=root
TimeoutStartSec=120

# /etc/systemd/system/thread-active-exporter.timer
[Unit]
Description=Thread active health exporter timer

[Timer]
OnBootSec=30
OnUnitActiveSec=120
AccuracySec=10

[Install]
WantedBy=timers.target
```

## Tools

| Tool | Description |
|------|-------------|
| `get_network_status` | Device state, role, network name, channel, leader info |
| `get_dataset` | Active operational dataset (credentials and parameters) |
| `get_ipaddresses` | All IPv6 addresses on the device |
| `get_topology` | Router table + neighbor table + child table |
| `get_router_table` | All routers with link quality and path costs |
| `get_neighbor_table` | Direct neighbors with RSSI |
| `get_child_table` | Children attached to this router |
| `get_network_data` | Border router prefixes, routes, and services |
| `get_counters` | MAC and MLE frame/error counters |
| `reset_counters` | Zero out MAC and MLE counters |
| `get_device_diagnostics` | Query a remote device's MAC counters and mode |
| `ping` | ICMPv6 ping by IPv6 address or RLOC16 |
| `scan` | Scan for nearby 802.15.4 networks |
| `thread_start` | Start Thread and join the network |
| `thread_stop` | Detach from the network |
| `device_reset` | Soft reset (dataset persists) |
| `factory_reset` | Erase all data and reset |
| `set_dataset_and_join` | Configure credentials and join a network |
| `get_preferred_role` | Current role, weight, and router thresholds |
| `set_preferred_role` | Change role to router, child, or leader |
| `set_leader_weight` | Set leader election weight (0-255) |
| `set_router_thresholds` | Control router promotion/demotion thresholds |
| `set_mode` | Set MLE mode flags (rx-on-idle, FTD, network data) |
| `dns_resolve` | Resolve hostname via Thread DNS |
| `dns_browse` | Browse DNS-SD services on the network |
| `dns_service` | Get details for a specific DNS-SD service instance |
| `get_link_metrics` | Enhanced link metrics (LQI, margin, RSSI) from a neighbor |
| `reset_device_counters` | Reset MAC counters on a remote device |
| `get_uptime` | Device uptime since last reset |
| `get_buffer_info` | Message buffer utilisation |
| `get_sockets` | Open UDP/TCP sockets (netstat) |
| `request_command` | Request a new CLI command be added to the server |

No raw CLI access is exposed. If the AI needs a command that isn't available, it uses `request_command` to describe what it needs so you can decide whether to add it.

## Hardware

Designed for the Arduino Nano Matter (Silicon Labs MGM240SD22VNA) running OpenThread CLI firmware from [swannman/ot-efr32](https://github.com/swannman/ot-efr32) (branch `arduino-nano-matter`).

The board exposes two USB CDC interfaces. On Linux, these appear as two serial ports:

- `/dev/ttyACM0` — RA4M1 bridge (not used for CLI)
- `/dev/ttyACM1` — MGM240 OpenThread CLI

On macOS only one port is visible:
- `/dev/cu.usbmodem*` — OpenThread CLI
