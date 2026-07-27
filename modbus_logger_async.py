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
import time
from typing import Any, Dict, List

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.pdu import ExceptionResponse

from modbus_common import (
    log, load_config, normalize_tasks, validate_tasks,
    DBManager,
    normalize_modbus_address,
    _CALL_MAP, _STORE_FUNCS,
    parse_response, store_result,
)

# Aliases matching the names used in existing tests for this module
CALL_MAP = _CALL_MAP
STORE_FUNCS = _STORE_FUNCS


# ---------- Async Modbus client ----------

class ModbusLoggerAsync:
    """Async Modbus client wrapper — async counterpart of ModbusClientWrapper."""

    def __init__(self, conn: Dict[str, Any], verbose: bool = False):
        self.verbose = verbose
        self._conn = conn
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
        try:
            return self._unwrap(await self.client.read_coils(address=address, count=count, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_discrete_inputs(self, unit: int, address: int, count: int, **_):
        try:
            return self._unwrap(await self.client.read_discrete_inputs(address=address, count=count, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_holding_registers(self, unit: int, address: int, count: int, **_):
        try:
            return self._unwrap(await self.client.read_holding_registers(address=address, count=count, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_input_registers(self, unit: int, address: int, count: int, **_):
        try:
            return self._unwrap(await self.client.read_input_registers(address=address, count=count, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def write_single_register(self, unit: int, address: int, value: int, **_):
        try:
            return self._unwrap(await self.client.write_register(address=address, value=value, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def write_holding_registers(self, unit: int, address: int, values: List[int], **_):
        try:
            return self._unwrap(await self.client.write_registers(address=address, values=values, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_device_information(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.read_device_information(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def write_coil(self, unit: int, address: int, value: bool, **_):
        try:
            return self._unwrap(await self.client.write_coil(address=address, value=value, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def write_coils(self, unit: int, address: int, values: List[bool], **_):
        try:
            return self._unwrap(await self.client.write_coils(address=address, values=values, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_exception_status(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.read_exception_status(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_query_data(self, unit: int, msg: bytes, **_):
        try:
            return self._unwrap(await self.client.diag_query_data(msg=msg, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_restart_communication(self, unit: int, toggle: bool, **_):
        try:
            return self._unwrap(await self.client.diag_restart_communication(toggle=toggle, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_diagnostic_register(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_diagnostic_register(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_device_identification(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.report_device_id(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_force_listen_only(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_force_listen_only(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_clear_counters(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_clear_counters(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_bus_message_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_bus_message_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_bus_comm_error_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_bus_comm_error_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_bus_exception_error_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_bus_exception_error_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_device_message_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_device_message_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_device_no_response_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_device_no_response_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_device_nak_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_device_nak_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_device_busy_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_device_busy_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_bus_char_overrun_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_bus_char_overrun_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_read_iop_overrun_count(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_read_iop_overrun_count(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_clear_overrun_counter(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_clear_overrun_counter(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_getclear_modbus_response(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_getclear_modbus_response(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_get_comm_event_counter(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_get_comm_event_counter(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_get_comm_event_log(self, unit: int, **_):
        try:
            return self._unwrap(await self.client.diag_get_comm_event_log(device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_fifo_queue(self, unit: int, queue_register_address: int, **_):
        try:
            return self._unwrap(await self.client.read_fifo_queue(address=queue_register_address, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def diag_change_ascii_input_delimeter(self, unit: int, data: int, **_):
        try:
            return self._unwrap(await self.client.diag_change_ascii_input_delimeter(data=data, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def read_file_record(self, unit: int, file_record, **_):
        try:
            return self._unwrap(await self.client.read_file_record(file_record=file_record, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def write_file_record(self, unit: int, file_record, **_):
        try:
            return self._unwrap(await self.client.write_file_record(file_record=file_record, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"

    async def readwrite_registers(self, unit: int, read_address: int, read_count: int,
                                  write_address: int, write_registers: List[int], **_):
        try:
            return self._unwrap(await self.client.readwrite_registers(
                read_address=read_address, read_count=read_count,
                write_address=write_address, write_registers=write_registers,
                device_id=unit,
            ))
        except Exception as e:
            return False, f"Exception: {e}"

    async def mask_write_register(self, unit: int, address: int, and_mask: int, or_mask: int, **_):
        try:
            return self._unwrap(await self.client.mask_write_register(address=address, and_mask=and_mask, or_mask=or_mask, device_id=unit))
        except Exception as e:
            return False, f"Exception: {e}"


# ---------- Query execution ----------

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

    if func not in _CALL_MAP:
        log(f"Unsupported function '{func}' in query '{name}'", verbose)
        return

    method_name, required_args = _CALL_MAP[func]
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

    parsed_value = parse_response(func, resp, query)
    store_result(db, name, func, parsed_value)

    log(f"Response <- name:{name} parsed:{parsed_value}", verbose)


# ---------- Per-task async runner ----------

async def _run_task(task: Dict[str, Any], client: "ModbusLoggerAsync", db: DBManager,
                    cfg: Dict[str, Any]) -> None:
    """Run one full task (all its queries) for a single cycle iteration."""
    verbose = cfg.get("verbose", False)
    address_base = int(task["connection"].get("address_base", 0))

    if not client.is_connected():
        log(f"Task '{task['name']}': connection lost — reconnecting...", True)
        if not await client.reconnect():
            log(f"Task '{task['name']}': reconnect failed; skipping.", True)
            return

    for q in task["queries"]:
        try:
            await execute_query(client, db, q, cfg, address_base)
        except Exception as e:
            log(f"Exception in task '{task['name']}' query '{q.get('name', q.get('function'))}': {e}", verbose)


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

    tasks: List[Dict[str, Any]] = []
    try:
        tasks = normalize_tasks(cfg)
    except ValueError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    errors = validate_tasks(tasks)
    if errors:
        for err in errors:
            print(f"Config error: {err}")
        sys.exit(1)

    db = DBManager(cfg.get("db_file", os.path.join(base_dir, "modbus_logger.db")), verbose=verbose)

    # Create and connect one async client per task
    clients: List[ModbusLoggerAsync] = []
    for task in tasks:
        client = ModbusLoggerAsync(task["connection"], verbose=verbose)
        if not await client.connect():
            print(f"Unable to connect for task '{task['name']}'.")
            for c in clients:
                await c.close()
            db.close()
            sys.exit(1)
        clients.append(client)

    # Pre-create data tables for all tasks
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
            cycle_start = time.monotonic()

            log(f"Starting cycle {cycle_count}", verbose)

            # Run all tasks concurrently within each cycle
            results = await asyncio.gather(
                *[_run_task(task, client, db, cfg) for task, client in zip(tasks, clients)],
                return_exceptions=True,
            )
            for task, result in zip(tasks, results):
                if isinstance(result, BaseException):
                    log(f"Task '{task['name']}' raised an unexpected exception: {result}", True)

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
        for client in clients:
            await client.close()
        db.close()
        log("Shutdown complete.", True)

if __name__ == "__main__":
    asyncio.run(main())
