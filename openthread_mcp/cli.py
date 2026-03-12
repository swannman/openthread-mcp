"""Low-level serial transport for OpenThread CLI."""

import logging
import threading
import time

import serial

logger = logging.getLogger(__name__)

# Default timeout waiting for a CLI response
DEFAULT_TIMEOUT = 5.0
# Longer timeout for commands that take time (scan, ping, diagnostics)
LONG_TIMEOUT = 15.0


class OTCLIError(Exception):
    """Raised when the CLI returns an error."""

    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


class OTCLIConnection:
    """Manages a serial connection to an OpenThread CLI device.

    Thread-safe: a lock serialises command execution so multiple MCP tool
    calls won't interleave on the wire.
    """

    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        self._ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
        # Give the RA4M1 bridge time to activate after SET_LINE_CODING
        time.sleep(1.5)
        # Drain any boot output
        self._ser.reset_input_buffer()
        logger.info("Serial connection opened on %s @ %d", self.port, self.baudrate)

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
            self._ser = None
            logger.info("Serial connection closed")

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def send_command(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Send a CLI command and return the response text.

        Blocks until 'Done' or 'Error' is received, or timeout expires.
        Raises OTCLIError on CLI errors, TimeoutError on timeout.
        """
        with self._lock:
            return self._send_command_locked(command, timeout)

    def _send_command_locked(self, command: str, timeout: float) -> str:
        if not self.is_open:
            self.open()

        ser = self._ser
        assert ser is not None

        # Flush stale data
        ser.reset_input_buffer()

        # Send command
        ser.write((command + "\r\n").encode())
        logger.debug("TX: %s", command)

        # Collect response lines until we see Done or Error
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        buf = b""

        while time.monotonic() < deadline:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            buf += chunk

            # Process complete lines
            while b"\r\n" in buf:
                line_bytes, buf = buf.split(b"\r\n", 1)
                line = line_bytes.decode(errors="replace").strip()

                # Skip echo of our command and empty prompts
                if line == command or line == ">" or line == "":
                    continue

                # Strip trailing prompt from the line
                if line.endswith(">"):
                    line = line[:-1].strip()
                    if not line:
                        continue

                if line == "Done":
                    result = "\n".join(lines)
                    logger.debug("RX: %s", result)
                    return result

                if line.startswith("Error"):
                    # Parse "Error <code>: <message>"
                    error_code = None
                    error_msg = line
                    try:
                        parts = line.split(":", 1)
                        error_code = int(parts[0].split()[1])
                        error_msg = parts[1].strip() if len(parts) > 1 else line
                    except (IndexError, ValueError):
                        pass
                    raise OTCLIError(error_msg, error_code)

                lines.append(line)

        raise TimeoutError(
            f"Timeout waiting for response to '{command}' after {timeout}s. "
            f"Partial output: {lines}"
        )
