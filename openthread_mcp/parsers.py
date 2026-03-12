"""Parsers for structured OT CLI output."""

import re


def parse_table(text: str) -> list[dict[str, str]]:
    """Parse a CLI table (router table, neighbor table, etc.) into dicts.

    Handles the pipe-delimited format with header row and separator rows.
    Returns a list of dicts keyed by header names.
    """
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    rows: list[dict[str, str]] = []

    # Find header line (first line with pipes that isn't a separator)
    header_line = None
    header_idx = -1
    for i, line in enumerate(lines):
        if "|" in line and "+" not in line and "-" not in line:
            header_line = line
            header_idx = i
            break

    if header_line is None:
        return rows

    headers = [h.strip() for h in header_line.split("|") if h.strip()]

    # Parse data rows (lines after header that have pipes, skip separators)
    for line in lines[header_idx + 1 :]:
        if "+" in line and "-" in line:
            continue
        if "|" not in line:
            continue
        values = [v.strip() for v in line.split("|") if v.strip()]
        if len(values) == len(headers):
            rows.append(dict(zip(headers, values)))

    return rows


def parse_key_value(text: str) -> dict[str, str]:
    """Parse colon-separated key: value output into a dict."""
    result: dict[str, str] = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def parse_counters(text: str) -> dict[str, int]:
    """Parse counter output (indented key: value with int values)."""
    result: dict[str, int] = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            try:
                result[key.strip()] = int(value)
            except ValueError:
                pass
    return result


def parse_ipaddrs(text: str) -> list[str]:
    """Parse ipaddr output into a list of IPv6 address strings."""
    return [line.strip() for line in text.strip().split("\n") if line.strip()]


def parse_network_data(text: str) -> dict[str, list[str]]:
    """Parse netdata show output into sections."""
    result: dict[str, list[str]] = {}
    current_section = None
    for line in text.strip().split("\n"):
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("fd") and not stripped.startswith("fc"):
            current_section = stripped[:-1]
            result[current_section] = []
        elif current_section is not None and stripped:
            result[current_section].append(stripped)
    return result


def parse_dataset(text: str) -> dict[str, str]:
    """Parse dataset active output into a dict."""
    result: dict[str, str] = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def parse_scan(text: str) -> list[dict[str, str]]:
    """Parse scan output table."""
    return parse_table(text)


def parse_diagnostic(text: str) -> dict[str, str | dict[str, int]]:
    """Parse networkdiagnostic get response.

    Returns a dict with top-level fields and a nested 'mac_counters' dict.
    """
    result: dict[str, str | dict[str, int]] = {}
    mac_counters: dict[str, int] = {}
    in_mac = False

    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("DIAG_GET"):
            continue
        if line == "MAC Counters:":
            in_mac = True
            continue
        if line == "Mode:":
            continue
        if in_mac and ":" in line:
            key, _, value = line.partition(":")
            try:
                mac_counters[key.strip()] = int(value.strip())
            except ValueError:
                in_mac = False
        if ":" in line and not in_mac:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()

    if mac_counters:
        result["mac_counters"] = mac_counters

    return result
