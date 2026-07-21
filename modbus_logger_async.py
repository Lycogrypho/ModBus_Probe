#!/usr/bin/env python3
"""
Modbus_Logger (async)

Async counterpart of modbus_logger.py. Sends Modbus requests based on config.json,
parses replies and stores results in SQLite using AsyncModbusTcpClient.

Requires: pymodbus v3.x. Uses only standard library otherwise.
"""

import argparse
import asyncio
import json
import os
import sys
import sqlite3
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.pdu import ExceptionResponse

# ---------- Utility helpers ----------

def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def log(msg: str, verbose: bool):
    if verbose:
        print(f"[{now_ts()}] {msg}")

# ---------- Config ----------

def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("verbose", False)
    cfg.setdefault("timeout", 5)
    cfg.setdefault("num_cycles", 0)
    cfg.setdefault("t_cycle", 30)
    cfg.setdefault("save_audit", False)
    return cfg

# ---------- SQLite ----------

class DBManager:
    def __init__(self, db_path: str, verbose: bool = False):
        self.db_path = db_path
        self.verbose = verbose
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.lock = threading.Lock()
        self._ensure_audit_table()

    def _ensure_audit_table(self):
        with self.lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_trail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    query TEXT NOT NULL,
                    is_write INTEGER NOT NULL
                )
            """)
            self.conn.commit()

    def ensure_data_table(self, table_name: str) -> str:
        safe = self._sanitize(table_name)
        with self.lock:
            self.conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {safe} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    value TEXT
                )
            """)
            self.conn.commit()
        return safe

    def insert_audit(self, query_serialized: str, is_write: bool):
        with self.lock:
            self.conn.execute(
                "INSERT INTO audit_trail (ts, query, is_write) VALUES (?, ?, ?)",
                (now_ts(), query_serialized, 1 if is_write else 0),
            )
            self.conn.commit()

    def insert_data(self, table_name: str, value: str):
        safe = self._sanitize(table_name)
        with self.lock:
            self.conn.execute(
                f"INSERT INTO {safe} (ts, value) VALUES (?, ?)", (now_ts(), value)
            )
            self.conn.commit()

    def _sanitize(self, name: str) -> str:
        safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
        if not safe:
            safe = "table"
        if safe[0].isdigit():
            safe = "_" + safe
        return safe

    def close(self):
        self.conn.close()

# ---------- Address normalization ----------

def normalize_modbus_address(address: Any, function: str, address_base: int = 0) -> int:
    """Convert Modbus address to 0-indexed PDU address. address_base in connection block."""
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

# ---------- Parsing ----------

def parse_bits(bitlist: List[bool]) -> str:
    return ",".join("1" if b else "0" for b in bitlist)

def decode_registers(registers: List[int], data_type: str, endian: str) -> Any:
    if not registers:
        return None
    data_type = {
        "REAL": "float32", "BOOL": "bool", "INT": "int16", "UINT": "uint16",
        "DINT": "int32", "UDINT": "uint32",
    }.get(data_type, data_type)
    try:
        if endian == "Little":
            raw = b"".join(struct.pack("<H", r & 0xFFFF) for r in registers)
            bo = "<"
        else:
            raw = b"".join(struct.pack(">H", r & 0xFFFF) for r in registers)
            bo = ">"
        if data_type == "bool":    return bool(registers[0])
        if data_type == "uint16":  return registers[0] & 0xFFFF
        if data_type == "int16":   return struct.unpack(bo + "h", raw[:2])[0]
        if data_type == "uint32":  return struct.unpack(bo + "I", raw[:4])[0]
        if data_type == "int32":   return struct.unpack(bo + "i", raw[:4])[0]
        if data_type == "float32": return struct.unpack(bo + "f", raw[:4])[0]
        if data_type == "float64": return struct.unpack(bo + "d", raw[:8])[0]
        if data_type == "hex":     return raw.hex()
        return registers
    except Exception:
        return registers

# ---------- Async Modbus client ----------

class ModbusLoggerAsync:
    """Async Modbus client wrapper — async counterpart of ModbusClientWrapper."""

    def __init__(self, cfg: Dict[str, Any], verbose: bool = False):
        self.verbose = verbose
        self._conn = cfg.get("connection", {})
        self.client = AsyncModbusTcpClient(
            host=self._conn.get("host", "127.0.0.1"),
            port=int(self._conn.get("port", 502)),
            timeout=self._conn.get("timeout", 5),
        )

    async def connect(self) -> bool:
        return await self.client.connect()

    async def close(self):
        self.client.close()

    def is_connected(self) -> bool:
        """Return True if the underlying TCP transport appears open."""
        if hasattr(self.client, "is_socket_open"):
            return self.client.is_socket_open()
        if hasattr(self.client, "connected"):
            return self.client.connected
        return True

    async def reconnect(self) -> bool:
        """Re-establish connection with linear back-off. Returns True on success."""
        retries = int(self._conn.get("reconnect_retries", 3))
        delay = float(self._conn.get("reconnect_delay", 5))
        try:
            self.client.close()
        except Exception:
            pass
        for attempt in range(1, retries + 1):
            log(f"Reconnect attempt {attempt}/{retries} (waiting {delay}s)...", self.verbose)
            await asyncio.sleep(delay)
            try:
                self.client = AsyncModbusTcpClient(
                    host=self._conn.get("host", "127.0.0.1"),
                    port=int(self._conn.get("port", 502)),
                    timeout=self._conn.get("timeout", 5),
                )
                if await self.client.connect():
                    log("Reconnect successful.", self.verbose)
                    return True
            except Exception as e:
                log(f"Reconnect attempt {attempt} raised: {e}", self.verbose)
        log("All reconnect attempts failed.", self.verbose)
        return False

    def _unwrap(self, resp):
        if resp is None:
            return False, "No response"
        if isinstance(resp, ExceptionResponse):
            return False, f"Modbus exception: {resp}"
        if getattr(resp, "isError", None) and resp.isError():
            return False, f"Error response: {resp}"
        return True, resp

    async def read_coils(self, unit: int, address: int, count: int, **_):
        return self._unwrap(await self.client.read_coils(address=address, count=count, device_id=unit))

    async def read_discrete_inputs(self, unit: int, address: int, count: int, **_):
        return self._unwrap(await self.client.read_discrete_inputs(address=address, count=count, device_id=unit))

    async def read_holding_registers(self, unit: int, address: int, count: int, **_):
        log("executing read_holding_registers", self.verbose)
        r = await self.client.read_holding_registers(address=address, count=count, device_id=unit)
        if not r.isError():
            for i, value in enumerate(r.registers):
                log(f"Register {address+i}: {value}", self.verbose)
        return self._unwrap(r)

    async def read_input_registers(self, unit: int, address: int, count: int, **_):
        return self._unwrap(await self.client.read_input_registers(address=address, count=count, device_id=unit))

    async def write_single_register(self, unit: int, address: int, value: int, **_):
        return self._unwrap(await self.client.write_register(address=address, value=value, device_id=unit))

    async def write_holding_registers(self, unit: int, address: int, values: List[int], **_):
        return self._unwrap(await self.client.write_registers(address=address, values=values, device_id=unit))

    async def read_device_information(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.read_device_information(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def write_coil(self, unit: int, address: int, value: bool, **_):
        return self._unwrap(await self.client.write_coil(address=address, value=value, device_id=unit))

    async def write_coils(self, unit: int, address: int, values: List[bool], **_):
        return self._unwrap(await self.client.write_coils(address=address, values=values, device_id=unit))

    async def read_exception_status(self, unit: int, **_):
        return self._unwrap(await self.client.read_exception_status(device_id=unit))

    async def diag_query_data(self, unit: int, msg: bytes, **_):
        return self._unwrap(await self.client.diag_query_data(msg=msg, device_id=unit))

    async def diag_restart_communication(self, unit: int, toggle: bool, **_):
        return self._unwrap(await self.client.diag_restart_communication(toggle=toggle, device_id=unit))

    async def read_diagnostic_register(self, unit: int, **_):
        return self._unwrap(await self.client.diag_read_diagnostic_register(device_id=unit))

    async def read_device_identification(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.report_device_id(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def mask_write_register(self, unit: int, address: int, and_mask: int, or_mask: int, **_):
        try:
            return self._unwrap(await self.client.mask_write_register(address=address, and_mask=and_mask, or_mask=or_mask, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

# ---------- Query execution ----------

CALL_MAP = {
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

STORE_FUNCS = frozenset({
    "read_coils", "read_discrete_inputs", "read_input_registers",
    "read_holding_registers", "read_exception_status", "read_diagnostic_register",
    "read_device_identification", "read_device_information",
})

async def execute_query(
    client: ModbusLoggerAsync,
    db: DBManager,
    query: Dict[str, Any],
    cfg: Dict[str, Any],
    address_base: int,
):
    verbose = cfg.get("verbose", False)
    func = query.get("function")
    unit = int(query.get("unit", cfg.get("unit", 1)))
    name = query.get("name", func)
    serialized = json.dumps(query, ensure_ascii=False)

    if func not in CALL_MAP:
        log(f"Unsupported function '{func}' in query '{name}'", verbose)
        return

    method_name, required_args = CALL_MAP[func]
    kw: Dict[str, Any] = {}
    for arg in required_args:
        if arg not in query:
            log(f"Missing argument '{arg}' for '{func}' in '{name}'", verbose)
            return
        kw[arg] = query[arg]

    if "address" in kw:
        kw["address"] = normalize_modbus_address(kw["address"], func, address_base)

    log(f"Query -> name:{name} function:{func} unit:{unit} params:{kw}", verbose)

    success, resp = await getattr(client, method_name)(unit=unit, **kw)

    is_write = func.startswith("write")
    if not success:
        log(f"Query '{name}' failed: {resp}", verbose)
        if cfg.get("save_audit", False) or is_write:
            db.insert_audit(serialized, is_write=is_write)
        return

    if is_write or cfg.get("save_audit", False):
        db.insert_audit(serialized, is_write=is_write)

    parsed_value: Any = None
    try:
        if func in ("read_coils", "read_discrete_inputs"):
            bits = getattr(resp, "bits", None) or getattr(resp, "bits_message", None)
            parsed_value = parse_bits(bits if bits is not None else [])
        elif func in ("read_holding_registers", "read_input_registers"):
            regs = getattr(resp, "registers", []) or []
            parsed_value = decode_registers(regs, query.get("data_type", "uint16"), query.get("endian", "Big"))
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

    if func in STORE_FUNCS:
        try:
            val = json.dumps(parsed_value, ensure_ascii=False) if not isinstance(parsed_value, (str, int, float, type(None))) else str(parsed_value)
        except Exception:
            val = str(parsed_value)
        db.insert_data(name, val)

    log(f"Response <- name:{name} parsed:{parsed_value}", verbose)

# ---------- Main loop ----------

async def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    parser = argparse.ArgumentParser(description="Modbus async data logger")
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

    db = DBManager(cfg.get("db_file", os.path.join(base_dir, "modbus_logger.db")), verbose=verbose)
    client = ModbusLoggerAsync(cfg, verbose=verbose)

    if not await client.connect():
        print("Unable to connect to Modbus server.")
        sys.exit(1)

    queries = cfg.get("queries", [])
    if not isinstance(queries, list) or not queries:
        log("No queries defined in config.json", verbose)
        await client.close()
        db.close()
        sys.exit(1)

    for q in queries:
        if q.get("function") in STORE_FUNCS:
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
            cycle_start = time.monotonic()

            log(f"Starting cycle {cycle_count}", verbose)

            if not client.is_connected():
                log("Connection lost — attempting reconnect...", True)
                if not await client.reconnect():
                    log("Reconnect failed; skipping cycle.", True)
                    elapsed = time.monotonic() - cycle_start
                    wait = t_cycle - elapsed
                    if wait > 0:
                        await asyncio.sleep(wait)
                    if num_cycles != 0 and cycle_count >= num_cycles:
                        break
                    continue

            for q in queries:
                try:
                    await execute_query(client, db, q, cfg, address_base)
                except Exception as e:
                    log(f"Exception in query '{q.get('name', q.get('function'))}': {e}", verbose)

            elapsed = time.monotonic() - cycle_start
            wait = t_cycle - elapsed
            if wait > 0:
                log(f"Cycle {cycle_count} done in {elapsed:.2f}s, sleeping {wait:.2f}s", verbose)
                await asyncio.sleep(wait)
            else:
                log(f"Cycle {cycle_count} took {elapsed:.2f}s (no sleep)", verbose)

    except asyncio.CancelledError:
        log("Cancelled.", True)
    except KeyboardInterrupt:
        log("Interrupted by user.", True)
    finally:
        await client.close()
        db.close()
        log("Shutdown complete.", True)

if __name__ == "__main__":
    asyncio.run(main())
