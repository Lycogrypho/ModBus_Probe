#!/usr/bin/env python3
"""
Modbus_Logger

Sends Modbus requests based on config.json, parses replies and stores results in SQLite.

Requires: pymodbus v3.x (already available). Uses only standard library otherwise.

Date: 2025-10-22
"""

import argparse
import json
import os
import sys
import sqlite3
import time
import struct
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# pymodbus imports (v3.x)
from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.pdu import ExceptionResponse


# OopCompanion:suppressRename

# ---------- Utility helpers ----------

def now_ts() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()

def log(msg: str, verbose: bool):
    """Print message depending on verbosity."""
    if verbose:
        print(f"[{now_ts()}] {msg}")

# ---------- Config handling ----------

def load_config(path: str) -> Dict[str, Any]:
    """Load and validate config.json from path."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("verbose", False)
    cfg.setdefault("timeout", 5)
    cfg.setdefault("num_cycles", 0)
    cfg.setdefault("t_cycle", 30)
    cfg.setdefault("save_audit", False)
    return cfg

# ---------- SQLite DB management ----------

class DBManager:
    """Manage SQLite connection and ensure required tables exist."""
    def __init__(self, db_path: str, verbose: bool = False):
        self.db_path = db_path
        self.verbose = verbose
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.lock = threading.Lock()
        self._ensure_audit_table()

    def _ensure_audit_table(self):
        """Create audit_trail table if not exists."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_trail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    query TEXT NOT NULL,
                    is_write INTEGER NOT NULL
                )
            """)
            self.conn.commit()

    def ensure_data_table(self, table_name: str):
        """Create a simple data table for a query: timestamp + value (TEXT)."""
        safe_name = self._sanitize_table(table_name)
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {safe_name} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    value TEXT
                )
            """)
            self.conn.commit()
        return safe_name

    def insert_audit(self, query_serialized: str, is_write: bool):
        """Insert an audit entry."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("INSERT INTO audit_trail (ts, query, is_write) VALUES (?, ?, ?)",
                        (now_ts(), query_serialized, 1 if is_write else 0))
            self.conn.commit()

    def insert_data(self, table_name: str, value: str):
        """Insert a value into a data table."""
        safe_name = self._sanitize_table(table_name)
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(f"INSERT INTO {safe_name} (ts, value) VALUES (?, ?)", (now_ts(), value))
            self.conn.commit()

    def _sanitize_table(self, name: str) -> str:
        """Sanitize table name (simple): replace non-alnum/_ with underscore."""
        safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
        if not safe:
            safe = "table"
        if safe[0].isdigit():
            safe = "_" + safe
        return safe

    def close(self):
        self.conn.close()

# ---------- Modbus client wrapper ----------

class ModbusClientWrapper:
    """
    Wraps pymodbus client for TCP or Serial as configured.
    Supports basic read/write functions and device id (43/14).
    """
    def __init__(self, cfg: Dict[str, Any], verbose: bool = False):
        self.cfg = cfg
        self.verbose = verbose
        self.client: Optional[ModbusBaseClient] = None
        self._create_client()

    def _create_client(self):
        """Create a pymodbus client according to config transport type."""
        transport = self.cfg.get("transport", "tcp").lower()
        timeout = self.cfg.get("timeout", 5)
        if transport == "tcp":
            host = self.cfg.get("host", "127.0.0.1")
            port = int(self.cfg.get("port", 502))
            self.client = ModbusTcpClient(host=host, port=port, timeout=timeout)
        elif transport == "serial":
            self.client = ModbusSerialClient(
                method=self.cfg.get("method", "rtu"),
                port=self.cfg.get("port", "/dev/ttyUSB0"),
                baudrate=int(self.cfg.get("baudrate", 19200)),
                timeout=timeout,
                bytesize=int(self.cfg.get("bytesize", 8)),
                parity=self.cfg.get("parity", "N"),
                stopbits=int(self.cfg.get("stopbits", 1))
            )
        else:
            raise ValueError(f"Unsupported transport: {transport}")

    def connect(self) -> bool:
        """Open client connection."""
        if self.client is None:
            self._create_client()
        return self.client.connect()

    def close(self):
        if self.client:
            self.client.close()

    def is_connected(self) -> bool:
        """Return True if the underlying transport appears open."""
        if self.client is None:
            return False
        if hasattr(self.client, "is_socket_open"):
            return self.client.is_socket_open()
        return True  # serial: assume open if connect() succeeded

    def reconnect(self) -> bool:
        """Re-establish connection with linear back-off. Returns True on success."""
        retries = int(self.cfg.get("reconnect_retries", 3))
        delay = float(self.cfg.get("reconnect_delay", 5))
        try:
            self.client.close()
        except Exception:
            pass
        for attempt in range(1, retries + 1):
            log(f"Reconnect attempt {attempt}/{retries} (waiting {delay}s)...", self.verbose)
            time.sleep(delay)
            try:
                self._create_client()
                if self.client.connect():
                    log("Reconnect successful.", self.verbose)
                    return True
            except Exception as e:
                log(f"Reconnect attempt {attempt} raised: {e}", self.verbose)
        log("All reconnect attempts failed.", self.verbose)
        return False

    # The following functions return a tuple: (success: bool, result_or_error)
    def read_coils(self, unit: int, address: int, count: int, **kwargs):
        r = self.client.read_coils(address=address, count=count, device_id=unit)
        return self._unwrap_response(r)

    def read_discrete_inputs(self, unit: int, address: int, count: int, **kwargs):
        r = self.client.read_discrete_inputs(address=address, count=count, device_id=unit, no_response_expected=False)
        return self._unwrap_response(r)
    
    def read_holding_registers(self, unit: int, address: int, count: int, **kwargs):
        log("executing read_holding_registers", self.verbose)
        r = self.client.read_holding_registers(address=int(address), count=int(count), device_id=unit, no_response_expected=False)

        if not r.isError():
            for i, value in enumerate(r.registers):
                log(f"Register {address+i}: {value}", self.verbose)
        return self._unwrap_response(r)

    def read_input_registers(self, unit: int, address: int, count: int, **kwargs):
        r = self.client.read_input_registers(address=address, count=count, device_id=unit)
        return self._unwrap_response(r)

    def write_coil(self, unit: int, address: int, value: bool, **kwargs):
        r = self.client.write_coil(address=address, value=value, device_id=unit)
        return self._unwrap_response(r)

    def write_coils(self, unit: int, address: int, values: List[bool], **kwargs):
        r = self.client.write_coils(address=address, values=values, device_id=unit)
        return self._unwrap_response(r)

    def write_single_register(self, unit: int, address: int, value: int, **kwargs):
        r = self.client.write_register(address=address, value=value, device_id=unit)
        return self._unwrap_response(r)

    def write_holding_registers(self, unit: int, address: int, values: List[int], **kwargs):
        r = self.client.write_registers(address=address, values=values, device_id=unit)
        return self._unwrap_response(r)

    def read_exception_status(self, unit: int, **kwargs):
        r = self.client.read_exception_status(device_id=unit)
        return self._unwrap_response(r)

    def diag_query_data(self, unit: int, msg: bytes, **kwargs):
        r = self.client.diag_query_data(msg=msg, device_id=unit)
        return self._unwrap_response(r)

    def diag_restart_communication(self, unit: int, toggle: bool, **kwargs):
        r = self.client.diag_restart_communication(toggle=toggle, device_id=unit)
        return self._unwrap_response(r)

    def read_diagnostic_register(self, unit: int, **kwargs):
        r = self.client.diag_read_diagnostic_register(device_id=unit)
        return self._unwrap_response(r)

    def read_device_identification(self, unit: int, **kwargs):
        try:
            r = self.client.report_device_id(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def read_device_information(self, unit: int, **kwargs):
        try:
            r = self.client.read_device_information(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def mask_write_register(self, unit: int, address: int, and_mask: int, or_mask: int, **kwargs):
        try:
            r = self.client.mask_write_register(address=address, and_mask=and_mask, or_mask=or_mask, device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def _unwrap_response(self, resp):
        """Normalize pymodbus response or error into (success, payload)."""
        if resp is None:
            return False, "No response (timeout or connection error)"
        if isinstance(resp, ExceptionResponse):
            return False, f"Modbus exception: {resp}"
        if getattr(resp, "isError", None) and resp.isError():
            return False, f"Error response: {resp}"
        return True, resp

# ---------- Address normalization ----------

def normalize_modbus_address(address: Any, function: str, address_base: int = 0) -> int:
    """
    Convert Modbus address to 0-indexed PDU address.

    address_base=0 (default, standard): 6-digit 400001 → PDU 0; small addrs used as-is.
    address_base=1 (1-based devices):   6-digit 400001 → PDU 1; small addr 1 → PDU 0.

    Configure via "address_base" inside the "connection" block in config.json.
    """
    addr = int(address)
    if addr <= 65535:
        return addr - address_base
    offsets = {
        "read_holding_registers":  400001,
        "write_single_register":   400001,
        "write_holding_registers": 400001,
        "mask_write_register":     400001,
        "read_input_registers":    300001,
        "read_discrete_inputs":    100001,
        "read_coils":              1,
        "write_single_coil":       1,
        "write_coils":             1,
    }
    return addr - offsets.get(function, 0) + address_base

# ---------- Parsing utilities ----------

_TYPE_ALIASES: Dict[str, str] = {
    "REAL":  "float32",
    "BOOL":  "bool",
    "INT":   "int16",
    "UINT":  "uint16",
    "DINT":  "int32",
    "UDINT": "uint32",
}

def parse_bits(bitlist: List[bool]) -> str:
    """Convert list of bools to compact string representation '0/1' CSV."""
    return ",".join("1" if b else "0" for b in bitlist)

def decode_registers(registers: List[int], data_type: str, endian: str) -> Any:
    """
    Decode registers (list of 16-bit ints) into python value depending on data_type.
    Accepts both PLC-style names (REAL, BOOL, INT, UINT, DINT, UDINT) and
    low-level names (float32, int16, uint16, int32, uint32, float64, hex).
    endian: "Big" or "Little"
    """
    if not registers:
        return None
    data_type = _TYPE_ALIASES.get(data_type, data_type)
    try:
        if endian == "Little":
            raw = b"".join(struct.pack("<H", r & 0xFFFF) for r in registers)
            byteorder = "<"
        else:
            raw = b"".join(struct.pack(">H", r & 0xFFFF) for r in registers)
            byteorder = ">"
        if data_type == "bool":
            return bool(registers[0])
        if data_type == "uint16":
            return registers[0] & 0xFFFF
        if data_type == "int16":
            return struct.unpack(byteorder + "h", raw[:2])[0]
        if data_type == "uint32":
            return struct.unpack(byteorder + "I", raw[:4])[0]
        if data_type == "int32":
            return struct.unpack(byteorder + "i", raw[:4])[0]
        if data_type == "float32":
            return struct.unpack(byteorder + "f", raw[:4])[0]
        if data_type == "float64":
            return struct.unpack(byteorder + "d", raw[:8])[0]
        if data_type == "hex":
            return raw.hex()
        return registers
    except Exception:
        return registers

# ---------- Main execution logic ----------

_STORE_FUNCS = frozenset({
    "read_coils", "read_discrete_inputs", "read_input_registers",
    "read_holding_registers", "read_exception_status", "read_diagnostic_register",
    "read_device_identification", "read_device_information",
})

_CALL_MAP: Dict[str, tuple] = {
    "read_coils":                  ("read_coils",                  ["address", "count"]),
    "read_discrete_inputs":        ("read_discrete_inputs",        ["address", "count"]),
    "read_holding_registers":      ("read_holding_registers",      ["address", "count"]),
    "read_input_registers":        ("read_input_registers",        ["address", "count"]),
    "write_coil":                  ("write_coil",                  ["address", "value"]),
    "write_coils":                 ("write_coils",                 ["address", "values"]),
    "write_single_register":       ("write_single_register",       ["address", "value"]),
    "write_holding_registers":     ("write_holding_registers",     ["address", "values"]),
    "read_exception_status":       ("read_exception_status",       []),
    "read_diagnostic_register":    ("read_diagnostic_register",    []),
    "diag_query_data":             ("diag_query_data",             ["msg"]),
    "diag_restart_communication":  ("diag_restart_communication",  ["toggle"]),
    "read_device_identification":  ("read_device_identification",  []),
    "read_device_information":     ("read_device_information",     []),
    "mask_write_register":         ("mask_write_register",         ["address", "and_mask", "or_mask"]),
}

def execute_query(client: ModbusClientWrapper, db: DBManager, query: Dict[str, Any], cfg: Dict[str, Any], address_base: int = 0):
    """
    Execute single query dict. Query keys expected:
      - name: unique name for storage
      - function: one of supported function names
      - unit: modbus unit id
      - address / count / value / values / data_type / endian (where relevant)
    """
    verbose = cfg.get("verbose", False)
    func = query.get("function")
    unit = int(query.get("unit", cfg.get("unit", 1)))
    name = query.get("name", func)

    # Build a serialized representation for auditing
    serialized = json.dumps(query, ensure_ascii=False)

    if func not in _CALL_MAP:
        log(f"Unsupported function '{func}' in query '{name}'", verbose)
        return

    method_name, required_args = _CALL_MAP[func]
    kw = {}
    for arg in required_args:
        if arg not in query:
            log(f"Missing argument '{arg}' for function '{func}' in query '{name}'", verbose)
            return
        kw[arg] = query[arg]

    if "address" in kw:
        kw["address"] = normalize_modbus_address(kw["address"], func, address_base)

    if cfg.get("verbose", False):
        log(f"Query -> name:{name} function:{func} unit:{unit} params:{kw} required args:{required_args}", True)

    success, resp = getattr(client, method_name)(unit=unit, **kw)

    if not success:
        log(f"Query '{name}' failed: {resp}", verbose=True)
        is_write = func.startswith("write")
        if cfg.get("save_audit", False) or is_write:
            db.insert_audit(serialized, is_write=is_write)
        return

    parsed_value = None
    try:
        if func in ("read_coils", "read_discrete_inputs"):
            bits = getattr(resp, "bits", None)
            if bits is None and hasattr(resp, "bits_message"):
                bits = resp.bits_message
            parsed_value = parse_bits(bits if bits is not None else [])
        elif func in ("read_holding_registers", "read_input_registers"):
            regs = getattr(resp, "registers", []) or []
            endian = query.get("endian", "Big")
            data_type = query.get("data_type", "uint16")
            parsed_value = decode_registers(regs, data_type, endian)
        elif func in ("read_device_identification", "read_device_information"):
            if hasattr(resp, "information"):
                try:
                    parsed_value = {str(k): str(v) for k, v in resp.information.items()}
                except Exception:
                    parsed_value = str(resp)
            else:
                parsed_value = str(resp)
        elif func == "read_exception_status":
            parsed_value = getattr(resp, "status", str(resp))
        elif func == "read_diagnostic_register":
            parsed_value = getattr(resp, "registers", [str(resp)])[0] if hasattr(resp, "registers") else str(resp)
        else:
            parsed_value = str(resp)
    except Exception as e:
        parsed_value = f"Parse error: {e}"

    is_write = func.startswith("write")
    if is_write or cfg.get("save_audit", False):
        db.insert_audit(serialized, is_write=is_write)

    if func in _STORE_FUNCS:
        table_name = name
        try:
            val_to_store = json.dumps(parsed_value, ensure_ascii=False) if not isinstance(parsed_value, (str, int, float, type(None))) else str(parsed_value)
        except Exception:
            val_to_store = str(parsed_value)
        db.insert_data(table_name, val_to_store)

    if cfg.get("verbose", False):
        log(f"Response <- name:{name} parsed:{parsed_value}", True)
    else:
        log(f"Query '{name}' OK", True)

# ---------- Main loop ----------

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    parser = argparse.ArgumentParser(description="Modbus data logger")
    parser.add_argument("--config", metavar="PATH",
                        default=os.path.join(base_dir, "config.json"),
                        help="path to config.json (default: <script dir>/config.json)")
    parser.add_argument("-v", "--verbose", action="store_true", default=None,
                        help="enable verbose output (overrides config)")
    args = parser.parse_args()

    cfg_path = args.config
    if not os.path.exists(cfg_path):
        print(f"Config file not found: {cfg_path}")
        sys.exit(1)

    cfg = load_config(cfg_path)
    if args.verbose:
        cfg["verbose"] = True
    verbose = cfg.get("verbose", False)
    address_base = int(cfg.get("connection", {}).get("address_base", 0))

    db_file = cfg.get("db_file", os.path.join(base_dir, "modbus_logger.db"))
    db = DBManager(db_file, verbose=verbose)

    client = ModbusClientWrapper(cfg.get("connection", {}), verbose=verbose)
    if not client.connect():
        print("Unable to connect to Modbus server.")
        sys.exit(1)

    queries = cfg.get("queries", [])
    if not isinstance(queries, list) or len(queries) == 0:
        log("No queries defined in config.json", verbose)
        client.close()
        db.close()
        sys.exit(1)

    for q in queries:
        if q.get("function") in _STORE_FUNCS:
            db.ensure_data_table(q.get("name", q.get("function")))

    num_cycles = int(cfg.get("num_cycles", 0))
    t_cycle = float(cfg.get("t_cycle", 30))
    cycle_count = 0

    try:
        while True:
            if num_cycles != 0 and cycle_count >= num_cycles:
                log("Completed requested number of cycles. Exiting.", verbose)
                break
            cycle_count += 1
            cycle_start = time.time()

            if verbose:
                log(f"Starting cycle {cycle_count}", True)

            if not client.is_connected():
                log("Connection lost — attempting reconnect...", True)
                if not client.reconnect():
                    log("Reconnect failed; skipping cycle.", True)
                    elapsed = time.time() - cycle_start
                    wait = t_cycle - elapsed
                    if wait > 0:
                        time.sleep(wait)
                    if num_cycles != 0 and cycle_count >= num_cycles:
                        break
                    continue

            for q in queries:
                try:
                    execute_query(client, db, q, cfg, address_base)
                except Exception as e:
                    log(f"Exception executing query '{q.get('name', q.get('function'))}': {e}", verbose)

            elapsed = time.time() - cycle_start
            wait = t_cycle - elapsed
            if wait > 0:
                if verbose:
                    log(f"Cycle {cycle_count} done in {elapsed:.2f}s, sleeping {wait:.2f}s", True)
                time.sleep(wait)
            else:
                if verbose:
                    log(f"Cycle {cycle_count} took {elapsed:.2f}s (no sleep)", True)

    except KeyboardInterrupt:
        log("Interrupted by user.", True)
    finally:
        client.close()
        db.close()
        log("Shutdown complete.", True)

if __name__ == "__main__":
    main()
