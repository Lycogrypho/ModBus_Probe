# ModBus Probe

A lightweight Modbus data logger for Windows. Connects to one or more Modbus TCP or Serial (RTU) devices, reads registers and coils on a configurable cycle, decodes values into typed data, and persists results to SQLite.

Two entry points are provided:
- **`modbus_logger.py`** — synchronous, runs tasks sequentially. Suitable for most use cases.
- **`modbus_logger_async.py`** — async (`asyncio`), runs multiple device tasks concurrently within each cycle.

Both share the same config format, database schema, and command-line interface.

---

## Requirements

- Python 3.9+
- [pymodbus](https://pypi.org/project/pymodbus/) v3.x

```bash
pip install pymodbus
```

---

## Quick Start

### 1 — Configure

Copy `example_config.json` to `config.json` and edit it for your device(s):

```json
{
  "verbose": true,
  "num_cycles": 10,
  "t_cycle": 5,
  "db_file": "modbus_logger.db",
  "tasks": [
    {
      "name": "my_plc",
      "connection": {
        "transport": "tcp",
        "host": "192.168.0.10",
        "port": 502,
        "timeout": 5
      },
      "queries": [
        {
          "name": "temperature",
          "function": "read_holding_registers",
          "unit": 1,
          "address": 100,
          "count": 2,
          "data_type": "REAL",
          "endian": "Big"
        }
      ]
    }
  ]
}
```

### 2 — Run

```bash
# Sync logger (recommended for single device or sequential multi-device)
python modbus_logger.py

# Async logger (concurrent multi-device)
python modbus_logger_async.py

# Override config path
python modbus_logger.py --config C:\path\to\my_config.json

# Enable verbose output (overrides config)
python modbus_logger.py -v
```

Results are written to the SQLite database specified by `db_file`. Each query gets its own table named after the query's `name` field.

### 3 — Build Windows executable

```bash
pip install pyinstaller
pyinstaller --onefile --console --hidden-import=sqlite3 modbus_logger.py
# Output: dist/modbus_logger.exe
```

Deploy `dist/modbus_logger.exe` alongside `config.json`.

---

## Configuration Reference

### Global keys

| Key | Default | Description |
|-----|---------|-------------|
| `verbose` | `false` | Print timestamped log lines to stdout |
| `num_cycles` | `0` | Number of polling cycles (0 = run forever) |
| `t_cycle` | `30` | Cycle duration in seconds |
| `save_audit` | `false` | Write an audit row for every query (writes are always audited) |
| `db_file` | `modbus_logger.db` | SQLite database path |

### Config formats

**Multi-device (preferred)** — top-level `tasks` list, each task with its own `connection` and `queries`:

```json
{
  "tasks": [
    { "name": "device_A", "connection": { "transport": "tcp", "host": "192.168.0.10", "port": 502 }, "queries": [] },
    { "name": "device_B", "connection": { "transport": "tcp", "host": "192.168.0.20", "port": 502 }, "queries": [] }
  ]
}
```

**Single-device (legacy)** — top-level `connection` and `queries`. Automatically promoted to a one-task list; no migration needed.

### Connection block

| Key | Default | Description |
|-----|---------|-------------|
| `transport` | `"tcp"` | `"tcp"` or `"serial"` |
| `host` | `"127.0.0.1"` | TCP host (TCP only) |
| `port` | `502` | TCP port or serial port name |
| `timeout` | `5` | Request timeout in seconds |
| `address_base` | `0` | `0` = standard 0-indexed PDU; `1` = 1-based devices |
| `reconnect_retries` | `3` | Reconnect attempts on connection loss |
| `reconnect_delay` | `5` | Seconds to wait between reconnect attempts |

### Query block

| Key | Required | Description |
|-----|----------|-------------|
| `name` | Yes | Unique name — used as the SQLite table name |
| `function` | Yes | Modbus function name (see table below) |
| `unit` | Yes | Modbus unit/slave ID |
| `address` | Read/write | Register or coil address (0-indexed or PLC-style 6-digit) |
| `count` | Reads | Number of registers or coils to read |
| `value` / `values` | Writes | Value(s) to write |
| `data_type` | No | Decoding type (see table below); default `uint16` |
| `endian` | No | `"Big"` (default) or `"Little"` — byte order within each register |
| `word_order` | No | `"Big"` (default) or `"Little"` — register order for multi-register types |

### Supported functions

| Category | Functions |
|----------|-----------|
| Read | `read_coils`, `read_discrete_inputs`, `read_holding_registers`, `read_input_registers`, `read_exception_status`, `read_diagnostic_register`, `read_device_identification`, `read_device_information` |
| Write | `write_coil`, `write_coils`, `write_single_register`, `write_holding_registers`, `mask_write_register` |
| Diag | `diag_query_data`, `diag_restart_communication`, and 14 more diag counter/event functions |

### Supported data types

| PLC name | Low-level alias | Size |
|----------|----------------|------|
| `BOOL` | `bool` | 1 bit (register 0/1) |
| `INT` | `int16` | 1 register |
| `UINT` | `uint16` | 1 register |
| `DINT` | `int32` | 2 registers |
| `UDINT` | `uint32` | 2 registers |
| `REAL` | `float32` | 2 registers |
| — | `float64` | 4 registers |
| — | `hex` | raw hex string |
| — | `raw_registers` | `[uint16, ...]` list — no decoding |
| — | `probe` | dict with all 4 byte/word-order interpretations |

### Byte / word order

For 32- and 64-bit types, use `endian` and `word_order` together to match the server's layout:

| Layout | `endian` | `word_order` |
|--------|----------|-------------|
| ABCD (standard) | `"Big"` | `"Big"` |
| CDAB | `"Big"` | `"Little"` |
| BADC | `"Little"` | `"Big"` |
| DCBA (full reverse) | `"Little"` | `"Little"` |

Use `"data_type": "probe"` to log all four interpretations simultaneously when the server's byte order is unknown.

---

## Database Schema

```text
-- One row per successful read query
CREATE TABLE {query_name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,   -- UTC ISO-8601 timestamp
    value TEXT          -- decoded value (JSON for complex types)
);

-- Write operations and/or all ops if save_audit=true
CREATE TABLE audit_trail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    query TEXT NOT NULL,    -- full query JSON
    is_write INTEGER NOT NULL
);
```

Table names are sanitised: non-alphanumeric characters become `_`; leading digits get a `_` prefix.

---

## Project Structure

| File | Role |
|------|------|
| `modbus_logger.py` | Sync entry point — `ModbusClientWrapper` + sync `main()` |
| `modbus_logger_async.py` | Async entry point — `ModbusLoggerAsync` + async `main()` |
| `modbus_common.py` | Shared infrastructure — config, DB, decode, dispatch tables |
| `config.json` | Active configuration (not in repo) |
| `example_config.json` | Configuration template |
