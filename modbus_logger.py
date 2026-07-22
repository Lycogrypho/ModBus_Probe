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
import time
from typing import Any, Dict, List, Optional

# pymodbus imports (v3.x)
from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.pdu import ExceptionResponse

from modbus_common import (
    log, load_config, normalize_tasks,
    DBManager,
    normalize_modbus_address,
    _CALL_MAP, _STORE_FUNCS,
    parse_response, store_result,
)


# OopCompanion:suppressRename

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

    def diag_force_listen_only(self, unit: int, **kwargs):
        try:
            r = self.client.diag_force_listen_only(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_clear_counters(self, unit: int, **kwargs):
        try:
            r = self.client.diag_clear_counters(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_bus_message_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_bus_message_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_bus_comm_error_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_bus_comm_error_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_bus_exception_error_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_bus_exception_error_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_device_message_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_device_message_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_device_no_response_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_device_no_response_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_device_nak_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_device_nak_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_device_busy_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_device_busy_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_bus_char_overrun_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_bus_char_overrun_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_read_iop_overrun_count(self, unit: int, **kwargs):
        try:
            r = self.client.diag_read_iop_overrun_count(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_clear_overrun_counter(self, unit: int, **kwargs):
        try:
            r = self.client.diag_clear_overrun_counter(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_getclear_modbus_response(self, unit: int, **kwargs):
        try:
            r = self.client.diag_getclear_modbus_response(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_get_comm_event_counter(self, unit: int, **kwargs):
        try:
            r = self.client.diag_get_comm_event_counter(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_get_comm_event_log(self, unit: int, **kwargs):
        try:
            r = self.client.diag_get_comm_event_log(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def read_fifo_queue(self, unit: int, queue_register_address: int, **kwargs):
        try:
            r = self.client.read_fifo_queue(address=queue_register_address, device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def diag_change_ascii_input_delimeter(self, unit: int, data: int, **kwargs):
        try:
            r = self.client.diag_change_ascii_input_delimeter(data=data, device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def read_file_record(self, unit: int, file_record, **kwargs):
        try:
            r = self.client.read_file_record(file_record=file_record, device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def write_file_record(self, unit: int, file_record, **kwargs):
        try:
            r = self.client.write_file_record(file_record=file_record, device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    def readwrite_registers(self, unit: int, read_address: int, read_count: int,
                            write_address: int, write_registers: List[int], **kwargs):
        try:
            r = self.client.readwrite_registers(
                read_address=read_address, read_count=read_count,
                write_address=write_address, write_registers=write_registers,
                device_id=unit,
            )
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

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


# ---------- Main execution logic ----------

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

    if verbose:
        log(f"Query -> name:{name} function:{func} unit:{unit} params:{kw} required args:{required_args}", verbose)

    success, resp = getattr(client, method_name)(unit=unit, **kw)

    if not success:
        log(f"Query '{name}' failed: {resp}", verbose)
        is_write = func.startswith("write")
        if cfg.get("save_audit", False) or is_write:
            db.insert_audit(serialized, is_write=is_write)
        return

    is_write = func.startswith("write")
    if is_write or cfg.get("save_audit", False):
        db.insert_audit(serialized, is_write=is_write)

    parsed_value = parse_response(func, resp, query)
    store_result(db, name, func, parsed_value)

    log(f"Response <- name:{name} parsed:{parsed_value}", verbose)


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

    try:
        tasks = normalize_tasks(cfg)
    except ValueError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    db_file = cfg.get("db_file", os.path.join(base_dir, "modbus_logger.db"))
    db = DBManager(db_file, verbose=verbose)

    # Create and connect one client per task
    clients: List[ModbusClientWrapper] = []
    for task in tasks:
        client = ModbusClientWrapper(task["connection"], verbose=verbose)
        if not client.connect():
            print(f"Unable to connect for task '{task['name']}'.")
            for c in clients:
                c.close()
            db.close()
            sys.exit(1)
        clients.append(client)

    # Pre-create data tables for all tasks up front
    for task in tasks:
        for q in task["queries"]:
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

            for task, client in zip(tasks, clients):
                address_base = int(task["connection"].get("address_base", 0))

                if not client.is_connected():
                    log(f"Task '{task['name']}': connection lost — reconnecting...", True)
                    if not client.reconnect():
                        log(f"Task '{task['name']}': reconnect failed; skipping.", True)
                        continue

                for q in task["queries"]:
                    try:
                        execute_query(client, db, q, cfg, address_base)
                    except Exception as e:
                        log(f"Exception in task '{task['name']}' query '{q.get('name', q.get('function'))}': {e}", verbose)

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
        for client in clients:
            client.close()
        db.close()
        log("Shutdown complete.", True)

if __name__ == "__main__":
    main()
