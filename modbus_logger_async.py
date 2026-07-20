#!/usr/bin/env python3
"""
Modbus_Logger

Sends Modbus requests based on config.json, parses replies and stores results in SQLite.

Requires: pymodbus v3.x (already available). Uses only standard library otherwise.

Date: 2025-10-22
"""

import asyncio
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

# ---------- Async Modbus monitor ----------

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
                result = await self.client.read_holding_registers(0, 10)
                if not result.isError():
                    await self.process_data(result.registers)
                await asyncio.sleep(self.interval)
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(5)

    async def process_data(self, data):
        """Process received data."""
        print(f"Data: {data}")

    async def stop(self):
        """Stop monitoring."""
        self.running = False
        if self.client:
            self.client.close()

async def main():
    monitor = AsyncModbusMonitor('192.168.1.100')
    task = asyncio.create_task(monitor.start())
    await asyncio.sleep(10)
    await monitor.stop()

if __name__ == "__main__":
    asyncio.run(main())
