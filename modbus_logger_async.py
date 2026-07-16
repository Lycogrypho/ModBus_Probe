#!/usr/bin/env python3
"""
Modbus_Logger

Sends Modbus requests based on config.json, parses replies and stores results in SQLite.

Requires: pymodbus v3.x (already available). Uses only standard library otherwise.

Date: 2025-10-22
"""

import json
import os
import sys
import sqlite3
import time
import struct
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

# pymodbus imports (v3.x)
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.pdu import ExceptionResponse

# ---------- Utility helpers ----------

def now_ts() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.utcnow().isoformat() + "Z"

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

class AsyncModbusMonitor:
    """Continuously monitor Modbus devices."""
    
    def __init__(self, host, interval=1.0):
        self.host = host
        self.interval = interval
        self.running = False
        self.client = None
    
    async def start(self):
        """Start monitoring."""
        self.client = AsyncModbusTcpClient(self.host)
        await self.client.connect()
        self.running = True
        
        while self.running:
            try:
                # Read data
                result = await self.client.read_holding_registers(0, 10)
                if not result.isError():
                    await self.process_data(result.registers)
                
                # Wait before next read
                await asyncio.sleep(self.interval)
                
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(5)  # Wait longer on error
    
    async def process_data(self, data):
        """Process received data."""
        print(f"Data: {data}")
        # Add your processing here
    
    async def stop(self):
        """Stop monitoring."""
        self.running = False
        if self.client:
            self.client.close()

# Use monitor
async def main():
    monitor = AsyncModbusMonitor('192.168.1.100')
    
    # Run for 10 seconds
    task = asyncio.create_task(monitor.start())
    await asyncio.sleep(10)
    await monitor.stop()

asyncio.run(main())
# ---------- Parsing utilities ----------

def parse_bits(bitlist: List[bool]) -> str:
    """Convert list of bools to compact string representation '0/1' CSV."""
    return ",".join("1" if b else "0" for b in bitlist)

def decode_registers(registers: List[int], data_type: str, endian: str) -> Any:
    """
    Decode registers (list of 16-bit ints) into python value depending on data_type.
    data_type examples: "uint16", "int16", "uint32", "int32", "float32", "float64", "hex"
    endian: "Big" or "Little"
    """
    # Build raw bytes from registers. pymodbus gives registers as 16-bit integers.
    # For Big endian we pack each register >H (big-endian per word), for Little we pack <H.
    if not registers:
        return None
    try:
        if endian == "Little":
            raw = b"".join(struct.pack("<H", r & 0xFFFF) for r in registers)
            byteorder = "<"
        else:
            raw = b"".join(struct.pack(">H", r & 0xFFFF) for r in registers)
            byteorder = ">"
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

def execute_query(client: AsyncModbusMonitor, db: DBManager, query: Dict[str, Any], cfg: Dict[str, Any]):
    """
    Execute single query dict. Query keys expected:
      - name: unique name for storage
      - function: one of supported function names
      - unit: modbus unit id
      - address / count / value / values / data_type / endian (where relevant)
    """
    verbose = cfg.get("verbose", False)
    func = query.get("function")
    unit = int(query.get("unit", 1))
    name = query.get("name", func)

    # Build a serialized representation for auditing
    serialized = json.dumps(query, ensure_ascii=False)

    # Determine call and call args
    call_map = {
        "read_coils": ("read_coils", ["address", "count"]),
        "read_discrete_inputs": ("read_discrete_inputs", ["address", "count"]),
        "read_holding_registers": ("read_holding_registers", ["address", "count"]),
        "read_input_registers": ("read_input_registers", ["address", "count"]),
        "write_single_register": ("write_single_register", ["address", "value"]),
        "write_holding_registers": ("write_holding_registers", ["address", "values"]),
        "read_device_identification": ("read_device_identification", []),
        "mask_write_register": ("mask_write_register", ["address", "and_mask", "or_mask"]),
    }

    if func not in call_map:
        log(f"Unsupported function '{func}' in query '{name}'", verbose)
        return

    method_name, required_args = call_map[func]
    kw = {}
    for arg in required_args:
        if arg not in query:
            log(f"Missing argument '{arg}' for function '{func}' in query '{name}'", verbose)
            return
        kw[arg] = query[arg]

    if cfg.get("verbose", False):
        log(f"Query -> name:{name} function:{func} unit:{unit} params:{kw}", True)

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
        elif func == "read_device_identification":
            info = {}
            if hasattr(resp, "information"):
                try:
                    for k, v in resp.information.items():
                        info[str(k)] = str(v)
                except Exception:
                    info = str(resp)
            else:
                info = str(resp)
            parsed_value = info
        elif func in ("write_single_register", "write_holding_registers", "mask_write_register"):
            parsed_value = str(resp)
        else:
            parsed_value = str(resp)
    except Exception as e:
        parsed_value = f"Parse error: {e}"

    is_write = func.startswith("write")
    if is_write or cfg.get("save_audit", False):
        db.insert_audit(serialized, is_write=is_write)

    if func in ("read_coils", "read_input_registers", "read_holding_registers", "read_device_identification"):
        table_name = name
        db.ensure_data_table(table_name)
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
    cfg_path = os.path.join(base_dir, "config.json")
    if not os.path.exists(cfg_path):
        print("config.json not found in script directory.")
        sys.exit(1)

    cfg = load_config(cfg_path)
    verbose = cfg.get("verbose", False)

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

    num_cycles = int(cfg.get("num_cycles", 0))
    t_cycle = float(cfg.get("t_cycle", 30))
    cycle_count = 0

    try:
        while True:
            cycle_start = time.time()
            cycle_count += 1
            if num_cycles != 0 and cycle_count > num_cycles:
                log("Completed requested number of cycles. Exiting.", verbose)
                break

            if verbose:
                log(f"Starting cycle {cycle_count}", True)

            for q in queries:
                try:
                    execute_query(client, db, q, cfg)
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

            if num_cycles != 0 and cycle_count >= num_cycles:
                log("Reached num_cycles; exiting loop.", verbose)
                break

    except KeyboardInterrupt:
        log("Interrupted by user.", True)
    finally:
        client.close()
        db.close()
        log("Shutdown complete.", True)

if __name__ == "__main__":
    main()