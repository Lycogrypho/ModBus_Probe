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
from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.client.base import ModbusBaseClient
from pymodbus.pdu import ExceptionResponse


# OopCompanion:suppressRename

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
        ''' read_coils(address: int, *, count: int = 1, device_id: int = 1, no_response_expected: bool = False) → T

    Read coils (code 0x01).

    Parameters:

            address – Start address to read from

            count – (optional) Number of coils to read

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –
        Discrete Inputs are addressed as 0-N (Note some device manuals uses 1-N, assuming 1==0).'''
        return self._unwrap_response(r)

    def read_discrete_inputs(self, unit: int, address: int, count: int, **kwargs):
        r = self.client.read_discrete_inputs(address=address, count=count, device_id=unit, no_response_expected=False)
        '''
         read_discrete_inputs(address: int, *, count: int = 1, device_id: int = 1, no_response_expected: bool = False) → T

    Read discrete inputs (code 0x02).

    Parameters:

            address – Start address to read from

            count – (optional) Number of coils to read

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –
        '''
        return self._unwrap_response(r)
    
    def read_holding_registers(self, unit: int, address: int, count: int, **kwargs):
        log("executing read_holding_registers", self.verbose)
        r = self.client.read_holding_registers(address=int(address), count=int(count), device_id=unit, no_response_expected=False)

        if not r.isError():
            for i, value in enumerate(r.registers):
                log(f"Register {address+i}: {value}", self.verbose)
        '''
         read_holding_registers(address: int, *, count: int = 1, device_id: int = 1, no_response_expected: bool = False) → T

    Read holding registers (code 0x03).

    Parameters:

            address – Start address to read from

            count – (optional) Number of registers to read

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –'''
        return self._unwrap_response(r)

    def read_input_registers(self, unit: int, address: int, count: int, **kwargs):
        r = self.client.read_input_registers(address=address, count=count, device_id=unit)
        '''
        
read_input_registers(address: int, *, count: int = 1, device_id: int = 1, no_response_expected: bool = False) → T

    Read input registers (code 0x04).

    Parameters:

            address – Start address to read from

            count – (optional) Number of registers to read

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to read from 1 to approx. 125 contiguous input registers in a remote device. The Request specifies the starting register address and the number of registers.

    Registers are addressed starting at zero. Therefore devices that specify 1-16 are addressed as 0-15.'''
        return self._unwrap_response(r)

    def write_coil(self):
        '''
        
write_coil(address: int, value: bool, *, device_id: int = 1, no_response_expected: bool = False) → T

    Write single coil (code 0x05).

    Parameters:

            address – Address to write to

            value – Boolean to write

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    write ON/OFF to a single coil in a remote device.

    Coils are addressed as 0-N (Note some device manuals uses 1-N, assuming 1==0).'''
    
    def write_coils(self):
        '''
        
write_coils(address: int, values: list[bool], *, device_id: int = 1, no_response_expected: bool = False) → T

    Write coils (code 0x0F).

    Parameters:

            address – Start address to write to

            values – List of booleans to write, or a single boolean to write

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    write ON/OFF to multiple coils in a remote device.

    Coils are addressed as 0-N (Note some device manuals uses 1-N, assuming 1==0).'''
    
    def write_single_register(self, unit: int, address: int, value: int, **kwargs):
        r = self.client.write_register(address=address, value=value, device_id=unit)
        '''
        
write_register(address: int, value: int, *, device_id: int = 1, no_response_expected: bool = False) → T

    Write register (code 0x06).

    Parameters:

            address – Address to write to

            value – Value to write

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to write a single holding register in a remote device.

    The Request specifies the address of the register to be written.

    Registers are addressed starting at zero. Therefore register numbered 1 is addressed as 0.'''
        return self._unwrap_response(r)

    def write_holding_registers(self, unit: int, address: int, values: List[int], **kwargs):
        r = self.client.write_registers(address=address, values=values, device_id=unit)
        '''

write_registers(address: int, values: list[int], *, device_id: int = 1, no_response_expected: bool = False) → T

    Write registers (code 0x10).

    Parameters:

            address – Start address to write to

            values – List of values to write

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to write a block of contiguous registers (1 to approx. 120 registers) in a remote device.
'''
        return self._unwrap_response(r)
        
    def read_exception_status(self):
        '''
read_exception_status(*, device_id: int = 1, no_response_expected: bool = False) → T

    Read Exception Status (code 0x07).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to read the contents of eight Exception Status outputs in a remote device.

    The function provides a simple method for accessing this information, because the Exception Output references are known (no output reference is needed in the function).

        '''
        
    def diag_query_data(self):
        '''
        
diag_query_data(msg: bytes, *, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose query data (code 0x08 sub 0x00).

    Parameters:

            msg – Message to be returned

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The data passed in the request data field is to be returned (looped back) in the response. The entire response message should be identical to the request.'''

    def diag_restart_communication(self):
        '''
diag_restart_communication(toggle: bool, *, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose restart communication (code 0x08 sub 0x01).

    Parameters:

            toggle – True if toggled.

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The remote device serial line port must be initialized and restarted, and all of its communications event counters are cleared. If the port is currently in Listen Only Mode, no response is returned. This function is the only one that brings the port out of Listen Only Mode. If the port is not currently in Listen Only Mode, a normal response is returned. This occurs before the restart is update_datastored.'''

    def read_diagnostic_register(self):
        '''
diag_read_diagnostic_register(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read diagnostic register (code 0x08 sub 0x02).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The contents of the remote device’s 16-bit diagnostic register are returned in the response.'''

    '''
    TODO:
    
diag_change_ascii_input_delimeter(*, delimiter: int = 10, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose change ASCII input delimiter (code 0x08 sub 0x03).

    Parameters:

            delimiter – char to replace LF

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The character passed in the request becomes the end of message delimiter for future messages (replacing the default LF character). This function is useful in cases of a Line Feed is not required at the end of ASCII messages.

diag_force_listen_only(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose force listen only (code 0x08 sub 0x04).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    Forces the addressed remote device to its Listen Only Mode for MODBUS communications.

    This isolates it from the other devices on the network, allowing them to continue communicating without interruption from the addressed remote device. No response is returned.

diag_clear_counters(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose clear counters (code 0x08 sub 0x0A).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    Clear ll counters and the diagnostic register. Also, counters are cleared upon power-up

diag_read_bus_message_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read bus message count (code 0x08 sub 0x0B).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of messages that the remote device has detected on the communications systems since its last restart, clear counters operation, or power-up

diag_read_bus_comm_error_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read Bus Communication Error Count (code 0x08 sub 0x0C).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of CRC errors encountered by the remote device since its last restart, clear counter operation, or power-up

diag_read_bus_exception_error_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read Bus Exception Error Count (code 0x08 sub 0x0D).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of modbus exception responses returned by the remote device since its last restart, clear counters operation, or power-up

diag_read_device_message_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read device Message Count (code 0x08 sub 0x0E).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of messages addressed to the remote device, that the remote device has processed since its last restart, clear counters operation, or power-up

diag_read_device_no_response_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read device No Response Count (code 0x08 sub 0x0F).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of messages addressed to the remote device, that the remote device has processed since its last restart, clear counters operation, or power-up.

diag_read_device_nak_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read device NAK Count (code 0x08 sub 0x10).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of messages addressed to the remote device for which it returned a Negative ACKNOWLEDGE (NAK) exception response, since its last restart, clear counters operation, or power-up. Exception responses are described and listed in section 7 .

diag_read_device_busy_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read device Busy Count (code 0x08 sub 0x11).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of messages addressed to the remote device for which it returned device Busy exception response, since its last restart, clear counters operation, or power-up.

diag_read_bus_char_overrun_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read Bus Character Overrun Count (code 0x08 sub 0x12).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    The response data field returns the quantity of messages addressed to the remote device that it could not handle due to a character overrun condition, since its last restart, clear counters operation, or power-up. A character overrun is caused by data characters arriving at the port faster than they can be stored, or by the loss of a character due to a hardware malfunction.

diag_read_iop_overrun_count(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose read Iop overrun count (code 0x08 sub 0x13).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    An IOP overrun is caused by data characters arriving at the port faster than they can be stored, or by the loss of a character due to a hardware malfunction. This function is specific to the 884.

diag_clear_overrun_counter(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose Clear Overrun Counter and Flag (code 0x08 sub 0x14).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    An error flag should be cleared, but nothing else in the specification mentions is, so it is ignored.

diag_getclear_modbus_response(*, data: int = 0, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose Get/Clear modbus plus (code 0x08 sub 0x15).

    Parameters:

            data – “Get Statistics” or “Clear Statistics”

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    In addition to the Function code (08) and Subfunction code (00 15 hex) in the query, a two-byte Operation field is used to specify either a “Get Statistics” or a “Clear Statistics” operation. The two operations are exclusive - the “Get” operation cannot clear the statistics, and the “Clear” operation does not return statistics prior to clearing them. Statistics are also cleared on power-up of the device,

diag_get_comm_event_counter(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose get event counter (code 0x0B).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to get a status word and an event count from the remote device.

    By fetching the current count before and after a series of messages, a client can determine whether the messages were handled normally by the remote device.

    The device’s event counter is incremented once for each successful message completion. It is not incremented for exception responses, poll commands, or fetch event counter commands.

    The event counter can be reset by means of the Diagnostics function Restart Communications or Clear Counters and Diagnostic Register.

diag_get_comm_event_log(*, device_id: int = 1, no_response_expected: bool = False) → T

    Diagnose get event counter (code 0x0C).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to get a status word.

    Event count, message count, and a field of event bytes from the remote device.

    The status word and event counts are identical to that returned by the Get Communications Event Counter function.

    The message counter contains the quantity of messages processed by the remote device since its last restart, clear counters operation, or power-up. This count is identical to that returned by the Diagnostic function Return Bus Message Count.

    The event bytes field contains 0-64 bytes, with each byte corresponding to the status of one MODBUS send or receive operation for the remote device. The remote device enters the events into the field in chronological order. Byte 0 is the most recent event. Each new byte flushes the oldest byte from the field.


    '''
    
    def read_device_identification(self):
        '''
        
report_device_id(*, device_id: int = 1, no_response_expected: bool = False) → T

    Report device ID (code 0x11).

    Parameters:

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to read the description of the type, the current status and other information specific to a remote device.'''
    
    def read_device_information(self, unit: int, **kwargs):
        # Use MEI type for Read Device Information; pymodbus provides read_device_information
        '''
read_device_information(*, read_code: int | None = None, object_id: int = 0, device_id: int = 1, no_response_expected: bool = False) → T

    Read FIFO queue (code 0x2B sub 0x0E).

    Parameters:

            read_code – The device information read code

            object_id – The object to read from

            device_id – (optional) Device id

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function allows reading the identification and additional information relative to the physical and functional description of a remote device, only.

    The Read Device Identification interface is modeled as an address space composed of a set of addressable data elements. The data elements are called objects and an object Id identifies them.'''
        try:
            r = self.client.read_device_information(device_id=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"

    '''
    
read_file_record(records: list[FileRecord], *, device_id: int = 1, no_response_expected: bool = False) → T

    Read file record (code 0x14).

    Parameters:

            records – List of FileRecord (Reference type, File number, Record Number)

            device_id – device id

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to perform a file record read. All request data lengths are provided in terms of number of bytes and all record lengths are provided in terms of registers.

    A file is an organization of records. Each file contains 10000 records, addressed 0000 to 9999 decimal or 0x0000 to 0x270f. For example, record 12 is addressed as 12. The function can read multiple groups of references. The groups can be separating (non-contiguous), but the references within each group must be sequential. Each group is defined in a separate “sub-request” field that contains seven bytes:

    The reference type: 1 byte
    The file number: 2 bytes
    The starting record number within the file: 2 bytes
    The length of the record to be read: 2 bytes

    The quantity of registers to be read, combined with all other fields in the expected response, must not exceed the allowable length of the MODBUS PDU: 235 bytes.

write_file_record(records: list[FileRecord], *, device_id: int = 1, no_response_expected: bool = False) → T

    Write file record (code 0x15).

    Parameters:

            records – List of File_record (Reference type, File number, Record Number, Record Length, Record Data)

            device_id – (optional) Device id

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to perform a file record write. All request data lengths are provided in terms of number of bytes and all record lengths are provided in terms of the number of 16 bit words.'''
    
    def mask_write_register(self, unit: int, address: int, and_mask: int, or_mask: int, **kwargs):
        '''
        
mask_write_register(*, address: int = 0, and_mask: int = 65535, or_mask: int = 0, device_id: int = 1, no_response_expected: bool = False) → T

    Mask write register (code 0x16).

    Parameters:

            address – The mask pointer address (0x0000 to 0xffff)

            and_mask – The and bitmask to apply to the register address

            or_mask – The or bitmask to apply to the register address

            device_id – (optional) device id

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function is used to modify the contents of a specified holding register using a combination of an AND mask, an OR mask, and the register’s current contents.

    The function can be used to set or clear individual bits in the register.'''
        try:
            r = self.client.mask_write_register(address, and_mask, or_mask) # ,unit=unit)
            return self._unwrap_response(r)
        except Exception as e:
            return False, f"Exception: {e}"


    '''
    TODO:
    
    
readwrite_registers(*, read_address: int = 0, read_count: int = 0, write_address: int = 0, address: int | None = None, values: list[int] | None = None, device_id: int = 1, no_response_expected: bool = False) → T

    Read/Write registers (code 0x17).

    Parameters:

            read_address – The address to start reading from

            read_count – The number of registers to read from address

            write_address – The address to start writing to

            address – (optional) use as read/write address

            values – List of values to write, or a single value to write

            device_id – (optional) Modbus device ID

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function performs a combination of one read operation and one write operation in a single MODBUS transaction. The write operation is performed before the read.

    Holding registers are addressed starting at zero. Therefore holding registers 1-16 are addressed in the PDU as 0-15.

read_fifo_queue(*, address: int = 0, device_id: int = 1, no_response_expected: bool = False) → T

    Read FIFO queue (code 0x18).

    Parameters:

            address – The address to start reading from

            device_id – (optional) device id

            no_response_expected – (optional) The client will not expect a response to the request

    Raises:

        ModbusException –

    This function allows to read the contents of a First-In-First-Out (FIFO) queue of register in a remote device. The function returns a count of the registers in the queue, followed by the queued data. Up to 32 registers can be read: the count, plus up to 31 queued data registers.

    The queue count register is returned first, followed by the queued data registers. The function reads the queue contents, but does not clear them.'''
    
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
    # Map PLC-style names to internal names
    data_type = {
        "REAL":  "float32",
        "BOOL":  "bool",
        "INT":   "int16",
        "UINT":  "uint16",
        "DINT":  "int32",
        "UDINT": "uint32",
    }.get(data_type, data_type)
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

def execute_query(client: ModbusClientWrapper, db: DBManager, query: Dict[str, Any], cfg: Dict[str, Any]):
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

    # Determine call and call args
    call_map = {
        "read_coils": ("read_coils", ["address", "count"]),
        "read_discrete_inputs": ("read_discrete_inputs", ["address", "count"]),
        "read_holding_registers": ("read_holding_registers", ["address", "count"]),
        "read_input_registers": ("read_input_registers", ["address", "count"]),
        "write_single_register": ("write_single_register", ["address", "value"]),
        "write_holding_registers": ("write_holding_registers", ["address", "values"]),
        "read_device_identification": ("read_device_information", []),
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

    if "address" in kw:
        address_base = int(cfg.get("connection", {}).get("address_base", 0))
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
        elif func == "read_device_information":
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

    if func in ("read_coils", "read_discrete_inputs", "read_input_registers", "read_holding_registers", "read_device_information"):
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