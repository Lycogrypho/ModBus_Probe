#!/usr/bin/env python3
"""
modbus_common.py — shared infrastructure for modbus_logger.py and modbus_logger_async.py.

Contains everything that is not specific to the sync or async transport:
  - Utility helpers: now_ts, log
  - Config loading: load_config
  - Database layer: DBManager
  - Address/type helpers: normalize_modbus_address, parse_bits, decode_registers, _TYPE_ALIASES
  - Dispatch tables: _CALL_MAP, _STORE_FUNCS
  - Response helpers: parse_response, store_result
"""

import json
import sqlite3
import struct
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List


# OopCompanion:suppressRename


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


def normalize_tasks(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Return a uniform list of task dicts from either config format.

    New format  — cfg contains a "tasks" list, each item has "connection" and "queries".
    Legacy format — cfg has top-level "connection" and "queries"; wrapped into one task.

    Every task dict is guaranteed to have "name", "connection", and "queries" keys.
    Global defaults (address_base etc.) already live inside each connection block.
    """
    if "tasks" in cfg:
        tasks = cfg["tasks"]
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("'tasks' must be a non-empty list")
        result = []
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                raise ValueError(f"tasks[{i}] must be an object")
            if "connection" not in t:
                raise ValueError(f"tasks[{i}] missing 'connection'")
            if "queries" not in t:
                raise ValueError(f"tasks[{i}] missing 'queries'")
            task = dict(t)
            task.setdefault("name", f"task_{i}")
            result.append(task)
        return result
    # Legacy: single connection + queries at top level
    if "connection" not in cfg or "queries" not in cfg:
        raise ValueError("Config must have either 'tasks' or both 'connection' and 'queries'")
    return [{"name": "default", "connection": cfg["connection"], "queries": cfg["queries"]}]


def validate_tasks(tasks: List[Dict[str, Any]]) -> List[str]:
    """
    Semantic validation of a normalized task list (output of normalize_tasks).

    Returns a list of human-readable error strings. An empty list means valid.
    Call after normalize_tasks() and exit before connecting if non-empty.
    """
    errors: List[str] = []
    seen_names: set = set()

    for task in tasks:
        tname = task.get("name", "unnamed")
        conn = task.get("connection", {})

        transport = conn.get("transport", "tcp")
        if transport not in ("tcp", "serial"):
            errors.append(
                f"Task '{tname}': connection.transport must be 'tcp' or 'serial', got {transport!r}"
            )

        if transport == "tcp":
            host = conn.get("host", "127.0.0.1")
            if not isinstance(host, str) or not host:
                errors.append(f"Task '{tname}': connection.host must be a non-empty string")
            port = conn.get("port", 502)
            try:
                p = int(port)
                if not (1 <= p <= 65535):
                    errors.append(f"Task '{tname}': connection.port must be 1–65535, got {port!r}")
            except (ValueError, TypeError):
                errors.append(f"Task '{tname}': connection.port must be a number, got {port!r}")

        timeout = conn.get("timeout", 5)
        try:
            if float(timeout) <= 0:
                errors.append(f"Task '{tname}': connection.timeout must be positive, got {timeout!r}")
        except (ValueError, TypeError):
            errors.append(f"Task '{tname}': connection.timeout must be a number, got {timeout!r}")

        queries = task.get("queries", [])
        if not queries:
            errors.append(f"Task '{tname}': 'queries' list must not be empty")

        for q in queries:
            qname = q.get("name", "")
            if not qname:
                errors.append(f"Task '{tname}': every query must have a non-empty 'name'")
            elif qname in seen_names:
                errors.append(
                    f"Task '{tname}': duplicate query name {qname!r} — names must be unique across all tasks"
                )
            else:
                seen_names.add(qname)

            func = q.get("function", "")
            if not func:
                errors.append(f"Task '{tname}' query '{qname}': 'function' is required")
                continue
            if func not in _CALL_MAP:
                errors.append(f"Task '{tname}' query '{qname}': unknown function {func!r}")
                continue
            _, required_args = _CALL_MAP[func]
            for arg in required_args:
                if arg not in q:
                    errors.append(
                        f"Task '{tname}' query '{qname}': function '{func}' requires argument '{arg}'"
                    )

    return errors


# ---------- SQLite ----------

# noinspection SqlNoDataSourceInspection
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
        safe = self._sanitize_table(table_name)
        with self.lock:
            self.conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {safe} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ref_ts TEXT NOT NULL,
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

    def insert_data(self, table_name: str, value: str, ref_time_stamp):
        safe = self._sanitize_table(table_name)
        with self.lock:
            self.conn.execute(
                f"INSERT INTO {safe} (ref_ts, ts, value) VALUES (?, ?, ?)", (ref_time_stamp, now_ts(), value)
            )
            self.conn.commit()

    def _sanitize_table(self, name: str) -> str:
        safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
        if not safe:
            safe = "table"
        if safe[0].isdigit():
            safe = "_" + safe
        return safe

    def close(self):
        self.conn.close()


# ---------- Address / type helpers ----------

_TYPE_ALIASES: Dict[str, str] = {
    "REAL":  "float32",
    "BOOL":  "bool",
    "INT":   "int16",
    "UINT":  "uint16",
    "DINT":  "int32",
    "UDINT": "uint32",
}


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


def parse_bits(bitlist: List[bool]) -> str:
    return ",".join("1" if b else "0" for b in bitlist)


def decode_registers(registers: List[int], data_type: str, endian: str = "Big",
                     word_order: str = "Big") -> Any:
    """
    Decode registers (list of 16-bit ints) into a Python value.

    endian ("Big" / "Little"): byte order within each 16-bit register.
      "Big" = standard Modbus (high byte first).
      "Little" = bytes are swapped within each register (BADC-style).
    word_order ("Big" / "Little"): order of registers for multi-register types.
      "Big" = high-word register first (default, ABCD).
      "Little" = low-word register first (CDAB / DCBA).
    Combined, the four standard layouts are:
      ABCD  endian=Big    word_order=Big
      CDAB  endian=Big    word_order=Little
      BADC  endian=Little word_order=Big
      DCBA  endian=Little word_order=Little
    Special data_type values:
      "raw_registers" — returns the uint16 register values as a list.
      "probe"         — returns a dict of all four layout interpretations.
    Accepts PLC-style names (REAL, BOOL, INT, UINT, DINT, UDINT) and
    low-level aliases (float32, int16, uint16, int32, uint32, float64, hex).
    """
    if not registers:
        return None

    if data_type == "raw_registers":
        return [r & 0xFFFF for r in registers]

    if data_type == "probe":
        return _probe_all_orderings(registers)

    data_type = _TYPE_ALIASES.get(data_type, data_type)
    endian = endian.capitalize()
    word_order = word_order.capitalize()

    # Apply word order: reverse the register list so the high-word is always first.
    regs = list(reversed(registers)) if word_order == "Little" else list(registers)

    try:
        # Normalise to canonical big-endian bytes:
        #   endian=Big    → pack ">H" (natural order, no change)
        #   endian=Little → pack "<H" (un-swap bytes within each register)
        # Always unpack with ">" so the final interpretation is consistent.
        if endian == "Little":
            raw = b"".join(struct.pack("<H", r & 0xFFFF) for r in regs)
        else:
            raw = b"".join(struct.pack(">H", r & 0xFFFF) for r in regs)

        if data_type == "bool":    return bool(regs[0])
        if data_type == "uint16":  return struct.unpack(">H", raw[:2])[0]
        if data_type == "int16":   return struct.unpack(">h", raw[:2])[0]
        if data_type == "uint32":  return struct.unpack(">I", raw[:4])[0]
        if data_type == "int32":   return struct.unpack(">i", raw[:4])[0]
        if data_type == "float32": return struct.unpack(">f", raw[:4])[0]
        if data_type == "float64": return struct.unpack(">d", raw[:8])[0]
        if data_type == "hex":     return raw.hex()
        return registers
    except Exception:
        return registers


def _probe_all_orderings(registers: List[int]) -> Dict[str, Any]:
    """Return all four byte/word-order interpretations of register data (for diagnostics)."""
    raw_be = b"".join(struct.pack(">H", r & 0xFFFF) for r in registers)
    result: Dict[str, Any] = {
        "raw_hex": raw_be.hex(),
        "raw_registers": [r & 0xFFFF for r in registers],
    }
    for label, endian, word_order in (
        ("ABCD", "Big",    "Big"),
        ("CDAB", "Big",    "Little"),
        ("BADC", "Little", "Big"),
        ("DCBA", "Little", "Little"),
    ):
        for typ in ("float32", "int32", "uint32"):
            try:
                result[f"{label}_{typ}"] = decode_registers(registers, typ, endian, word_order)
            except Exception:
                result[f"{label}_{typ}"] = None
    return result


def multiview_decode(registers: List[int], name: str, address: int) -> Dict[str, Any]:
    """
    Return all standard Modbus register representations for multiview (-m) mode.

    Per-register fields (one entry per register):
      binary      — 16-character string of '0'/'1' (unsigned)
      decimal     — signed int16 value (-32768 … 32767)
      hexadecimal — 4-digit lowercase hex string

    Multi-register float fields (None when too few registers are available).
    Keys match the four standard Modbus byte/word-order layouts:
      float32_abcd — endian=Big,    word_order=Big    (2 regs, standard)
      float32_cdab — endian=Big,    word_order=Little (2 regs, word-swapped)
      float32_badc — endian=Little, word_order=Big    (2 regs, byte-swapped)
      float32_dcba — endian=Little, word_order=Little (2 regs, full reverse)
      float64_abcd — same four layouts for 64-bit double (4 regs)
      float64_cdab
      float64_badc
      float64_dcba
    """
    regs = [r & 0xFFFF for r in registers]

    result: Dict[str, Any] = {
        "name":        name,
        "address":     address,
        "binary":      [f"{r:016b}" for r in regs],
        "decimal":     [struct.unpack(">h", struct.pack(">H", r))[0] for r in regs],
        "hexadecimal": [f"{r:04x}" for r in regs],
    }

    def _try_unpack(fmt: str, raw: bytes):
        try:
            return struct.unpack(fmt, raw)[0]
        except Exception:
            return None

    def _raw(reg_slice, endian_char: str) -> bytes:
        return b"".join(struct.pack(endian_char + "H", r) for r in reg_slice)

    if len(regs) >= 2:
        r2 = regs[:2]
        result["float32_abcd"] = _try_unpack(">f", _raw(r2,          ">"))
        result["float32_cdab"] = _try_unpack(">f", _raw(reversed(r2),">"))
        result["float32_badc"] = _try_unpack(">f", _raw(r2,          "<"))
        result["float32_dcba"] = _try_unpack(">f", _raw(reversed(r2),"<"))
    else:
        result["float32_abcd"] = result["float32_cdab"] = None
        result["float32_badc"] = result["float32_dcba"] = None

    if len(regs) >= 4:
        r4 = regs[:4]
        result["float64_abcd"] = _try_unpack(">d", _raw(r4,          ">"))
        result["float64_cdab"] = _try_unpack(">d", _raw(reversed(r4),">"))
        result["float64_badc"] = _try_unpack(">d", _raw(r4,          "<"))
        result["float64_dcba"] = _try_unpack(">d", _raw(reversed(r4),"<"))
    else:
        result["float64_abcd"] = result["float64_cdab"] = None
        result["float64_badc"] = result["float64_dcba"] = None

    return result


# ---------- Dispatch tables ----------

_CALL_MAP: Dict[str, tuple] = {
    "read_coils":                          ("read_coils",                          ["address", "count"]),
    "read_discrete_inputs":                ("read_discrete_inputs",                ["address", "count"]),
    "read_holding_registers":              ("read_holding_registers",              ["address", "count"]),
    "read_input_registers":                ("read_input_registers",                ["address", "count"]),
    "write_coil":                          ("write_coil",                          ["address", "value"]),
    "write_coils":                         ("write_coils",                         ["address", "values"]),
    "write_single_register":               ("write_single_register",               ["address", "value"]),
    "write_holding_registers":             ("write_holding_registers",             ["address", "values"]),
    "read_exception_status":               ("read_exception_status",               []),
    "read_diagnostic_register":            ("read_diagnostic_register",            []),
    "diag_query_data":                     ("diag_query_data",                     ["msg"]),
    "diag_restart_communication":          ("diag_restart_communication",          ["toggle"]),
    "diag_force_listen_only":              ("diag_force_listen_only",              []),
    "diag_clear_counters":                 ("diag_clear_counters",                 []),
    "diag_read_bus_message_count":         ("diag_read_bus_message_count",         []),
    "diag_read_bus_comm_error_count":      ("diag_read_bus_comm_error_count",      []),
    "diag_read_bus_exception_error_count": ("diag_read_bus_exception_error_count", []),
    "diag_read_device_message_count":      ("diag_read_device_message_count",      []),
    "diag_read_device_no_response_count":  ("diag_read_device_no_response_count",  []),
    "diag_read_device_nak_count":          ("diag_read_device_nak_count",          []),
    "diag_read_device_busy_count":         ("diag_read_device_busy_count",         []),
    "diag_read_bus_char_overrun_count":    ("diag_read_bus_char_overrun_count",    []),
    "diag_read_iop_overrun_count":         ("diag_read_iop_overrun_count",         []),
    "diag_clear_overrun_counter":          ("diag_clear_overrun_counter",          []),
    "diag_getclear_modbus_response":       ("diag_getclear_modbus_response",       []),
    "diag_get_comm_event_counter":         ("diag_get_comm_event_counter",         []),
    "diag_get_comm_event_log":             ("diag_get_comm_event_log",             []),
    "diag_change_ascii_input_delimeter":   ("diag_change_ascii_input_delimeter",   ["data"]),
    "read_file_record":                    ("read_file_record",                    ["file_record"]),
    "write_file_record":                   ("write_file_record",                   ["file_record"]),
    "readwrite_registers":                 ("readwrite_registers",                 ["read_address", "read_count", "write_address", "write_registers"]),
    "read_fifo_queue":                     ("read_fifo_queue",                     ["queue_register_address"]),
    "read_device_identification":          ("read_device_identification",          []),
    "read_device_information":             ("read_device_information",             []),
    "mask_write_register":                 ("mask_write_register",                 ["address", "and_mask", "or_mask"]),
}

_STORE_FUNCS = frozenset({
    "read_coils", "read_discrete_inputs", "read_input_registers",
    "read_holding_registers", "read_exception_status", "read_diagnostic_register",
    "read_device_identification", "read_device_information",
    "diag_read_bus_message_count", "diag_read_bus_comm_error_count",
    "diag_read_bus_exception_error_count", "diag_read_device_message_count",
    "diag_read_device_no_response_count", "diag_read_device_nak_count",
    "diag_read_device_busy_count", "diag_read_bus_char_overrun_count",
    "diag_read_iop_overrun_count", "diag_get_comm_event_counter",
    "diag_get_comm_event_log", "read_fifo_queue",
    "readwrite_registers", "read_file_record",
})


# ---------- Response helpers ----------

def parse_response(func: str, resp: Any, query: Dict[str, Any]) -> Any:
    """Extract a typed Python value from a successful Modbus response object."""
    try:
        if func in ("read_coils", "read_discrete_inputs"):
            bits = getattr(resp, "bits", None)
            if bits is None:
                bits = getattr(resp, "bits_message", None)
            return parse_bits(bits if bits is not None else [])
        if func in ("read_holding_registers", "read_input_registers"):
            regs = getattr(resp, "registers", []) or []
            return decode_registers(
                regs,
                query.get("data_type", "uint16"),
                query.get("endian", "Big"),
                query.get("word_order", "Big"),
            )
        if func in ("read_device_identification", "read_device_information"):
            if hasattr(resp, "information"):
                try:
                    return {str(k): str(v) for k, v in resp.information.items()}
                except Exception:
                    return str(resp)
            return str(resp)
        if func == "read_exception_status":
            return getattr(resp, "status", str(resp))
        if func in (
            "read_diagnostic_register", "diag_read_bus_message_count",
            "diag_read_bus_comm_error_count", "diag_read_bus_exception_error_count",
            "diag_read_device_message_count", "diag_read_device_no_response_count",
            "diag_read_device_nak_count", "diag_read_device_busy_count",
            "diag_read_bus_char_overrun_count", "diag_read_iop_overrun_count",
            "diag_get_comm_event_counter",
        ):
            return getattr(resp, "registers", [str(resp)])[0] if hasattr(resp, "registers") else str(resp)
        if func in ("diag_get_comm_event_log", "read_file_record", "readwrite_registers", "read_fifo_queue"):
            return str(resp)
        return str(resp)
    except Exception as e:
        return f"Parse error: {e}"


def store_result(db: DBManager, name: str, func: str, parsed_value: Any, ref_time_stamp) -> None:
    """Write parsed_value to the data table for name, only for read/query functions."""
    if func not in _STORE_FUNCS:
        return
    try:
        val = (
            json.dumps(parsed_value, ensure_ascii=False)
            if not isinstance(parsed_value, (str, int, float, type(None)))
            else str(parsed_value)
        )
    except Exception:
        val = str(parsed_value)
    db.insert_data(name, val, ref_time_stamp)
