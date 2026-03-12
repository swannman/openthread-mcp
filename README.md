# openthread-mcp

MCP server for monitoring and managing a Thread network via the OpenThread CLI on an Arduino Nano Matter.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

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
| `send_command` | Raw CLI command for anything not covered above |

## Hardware

Designed for the Arduino Nano Matter (Silicon Labs MGM240SD22VNA) running OpenThread CLI firmware from [swannman/ot-efr32](https://github.com/swannman/ot-efr32) (branch `arduino-nano-matter`).

The serial port name varies by OS:
- **macOS**: `/dev/cu.usbmodem*`
- **Linux** (pibot): `/dev/ttyACM0` (typically)
