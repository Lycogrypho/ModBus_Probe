"""
Tests for modbus_common shared helpers.
No pytest needed — run: py test_modbus_common.py
"""
import sys
import unittest
from unittest.mock import MagicMock, call, patch
import tempfile
import os


# ---------- parse_response tests ----------

class TestParseResponseCoils(unittest.TestCase):
    def setUp(self):
        from modbus_common import parse_response
        self.parse = parse_response

    def test_read_coils_bits_attribute(self):
        resp = MagicMock()
        resp.bits = [True, False, True]
        result = self.parse("read_coils", resp, {})
        self.assertEqual(result, "1,0,1")

    def test_read_coils_bits_message_fallback(self):
        resp = MagicMock(spec=[])
        resp.bits_message = [False, False, True]
        result = self.parse("read_coils", resp, {})
        self.assertEqual(result, "0,0,1")

    def test_read_discrete_inputs_empty_bits(self):
        resp = MagicMock(spec=[])
        result = self.parse("read_discrete_inputs", resp, {})
        self.assertEqual(result, "")

    def test_read_coils_none_bits_falls_back_to_empty(self):
        resp = MagicMock(spec=[])
        result = self.parse("read_coils", resp, {})
        self.assertEqual(result, "")


class TestParseResponseRegisters(unittest.TestCase):
    def setUp(self):
        from modbus_common import parse_response
        self.parse = parse_response

    def test_holding_registers_uint16_default(self):
        resp = MagicMock()
        resp.registers = [1234]
        result = self.parse("read_holding_registers", resp, {})
        self.assertEqual(result, 1234)

    def test_holding_registers_real(self):
        import struct
        raw = struct.pack(">f", 3.14)
        hi = struct.unpack(">H", raw[:2])[0]
        lo = struct.unpack(">H", raw[2:])[0]
        resp = MagicMock()
        resp.registers = [hi, lo]
        result = self.parse("read_holding_registers", resp, {"data_type": "REAL", "endian": "Big"})
        self.assertAlmostEqual(result, 3.14, places=5)

    def test_input_registers_uint16(self):
        resp = MagicMock()
        resp.registers = [42]
        result = self.parse("read_input_registers", resp, {"data_type": "uint16", "endian": "Big"})
        self.assertEqual(result, 42)

    def test_registers_empty_returns_none(self):
        resp = MagicMock()
        resp.registers = []
        result = self.parse("read_holding_registers", resp, {})
        self.assertIsNone(result)


class TestParseResponseDiag(unittest.TestCase):
    def setUp(self):
        from modbus_common import parse_response
        self.parse = parse_response

    def test_read_exception_status_returns_status(self):
        resp = MagicMock()
        resp.status = 0x03
        result = self.parse("read_exception_status", resp, {})
        self.assertEqual(result, 0x03)

    def test_read_diagnostic_register_with_registers(self):
        resp = MagicMock()
        resp.registers = [99]
        result = self.parse("read_diagnostic_register", resp, {})
        self.assertEqual(result, 99)

    def test_diag_counter_with_registers(self):
        resp = MagicMock()
        resp.registers = [7]
        result = self.parse("diag_read_bus_message_count", resp, {})
        self.assertEqual(result, 7)

    def test_diag_get_comm_event_log_returns_str(self):
        resp = MagicMock()
        resp.__str__ = lambda self: "event_log_str"
        result = self.parse("diag_get_comm_event_log", resp, {})
        self.assertEqual(result, "event_log_str")

    def test_read_file_record_returns_str(self):
        resp = MagicMock()
        resp.__str__ = lambda self: "file_record_data"
        result = self.parse("read_file_record", resp, {})
        self.assertEqual(result, "file_record_data")


class TestParseResponseDeviceInfo(unittest.TestCase):
    def setUp(self):
        from modbus_common import parse_response
        self.parse = parse_response

    def test_device_identification_with_information(self):
        resp = MagicMock()
        resp.information = {1: b"VendorName"}
        result = self.parse("read_device_identification", resp, {})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["1"], "b'VendorName'")

    def test_device_identification_without_information(self):
        class NoInfo:
            def __str__(self):
                return "raw_id"
        result = self.parse("read_device_identification", NoInfo(), {})
        self.assertEqual(result, "raw_id")

    def test_device_information_with_information(self):
        resp = MagicMock()
        resp.information = {0: "Acme"}
        result = self.parse("read_device_information", resp, {})
        self.assertEqual(result["0"], "Acme")


class TestParseResponseDefault(unittest.TestCase):
    def setUp(self):
        from modbus_common import parse_response
        self.parse = parse_response

    def test_unknown_func_returns_str(self):
        resp = MagicMock()
        resp.__str__ = lambda self: "raw_response"
        result = self.parse("some_unknown_function", resp, {})
        self.assertEqual(result, "raw_response")

    def test_parse_error_returns_error_string(self):
        class Boom:
            @property
            def bits(self):
                raise RuntimeError("exploded")
        result = self.parse("read_coils", Boom(), {})
        self.assertIn("Parse error", result)


# ---------- store_result tests ----------

class TestStoreResult(unittest.TestCase):
    def setUp(self):
        from modbus_common import store_result
        self.store = store_result

    def _db(self):
        db = MagicMock()
        return db

    def test_stores_for_read_holding_registers(self):
        db = self._db()
        self.store(db, "my_sensor", "read_holding_registers", 42)
        db.insert_data.assert_called_once_with("my_sensor", "42")

    def test_skips_for_write_function(self):
        db = self._db()
        self.store(db, "out", "write_coil", True)
        db.insert_data.assert_not_called()

    def test_skips_for_diag_write(self):
        db = self._db()
        self.store(db, "x", "diag_restart_communication", None)
        db.insert_data.assert_not_called()

    def test_stores_dict_as_json(self):
        db = self._db()
        self.store(db, "dev", "read_device_identification", {"1": "Acme"})
        db.insert_data.assert_called_once()
        stored_val = db.insert_data.call_args[0][1]
        self.assertIn("Acme", stored_val)

    def test_stores_none_as_string(self):
        db = self._db()
        self.store(db, "q", "read_exception_status", None)
        db.insert_data.assert_called_once_with("q", "None")

    def test_stores_float(self):
        db = self._db()
        self.store(db, "temp", "read_holding_registers", 3.14)
        db.insert_data.assert_called_once_with("temp", "3.14")


# ---------- _CALL_MAP / _STORE_FUNCS cross-module consistency ----------

class TestCallMapConsistency(unittest.TestCase):
    def test_sync_and_async_share_same_object(self):
        from modbus_common import _CALL_MAP as CM
        from modbus_logger import _CALL_MAP as SYNC_CM
        from modbus_logger_async import CALL_MAP as ASYNC_CM
        self.assertIs(CM, SYNC_CM)
        self.assertIs(CM, ASYNC_CM)

    def test_store_funcs_shared(self):
        from modbus_common import _STORE_FUNCS as SF
        from modbus_logger import _STORE_FUNCS as SYNC_SF
        from modbus_logger_async import STORE_FUNCS as ASYNC_SF
        self.assertIs(SF, SYNC_SF)
        self.assertIs(SF, ASYNC_SF)


# ---------- DBManager ----------

class TestDBManager(unittest.TestCase):
    def test_sanitize_table_replaces_special_chars(self):
        from modbus_common import DBManager
        db = DBManager(":memory:")
        self.assertEqual(db._sanitize_table("foo-bar.baz"), "foo_bar_baz")
        self.assertEqual(db._sanitize_table("1bad"), "_1bad")
        self.assertEqual(db._sanitize_table(""), "table")
        db.close()

    def test_ensure_data_table_creates_table(self):
        from modbus_common import DBManager
        db = DBManager(":memory:")
        safe = db.ensure_data_table("my_sensor")
        self.assertEqual(safe, "my_sensor")
        cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='my_sensor'")
        self.assertIsNotNone(cur.fetchone())
        db.close()

    def test_insert_data_and_audit(self):
        from modbus_common import DBManager
        db = DBManager(":memory:")
        db.ensure_data_table("t")
        db.insert_data("t", "hello")
        db.insert_audit('{"q": 1}', is_write=False)
        row = db.conn.execute("SELECT value FROM t").fetchone()
        self.assertEqual(row[0], "hello")
        audit = db.conn.execute("SELECT is_write FROM audit_trail").fetchone()
        self.assertEqual(audit[0], 0)
        db.close()


# ---------- word_order / probe / raw_registers ----------

class TestWordOrder(unittest.TestCase):
    """2.10 — word_order parameter in decode_registers."""

    def setUp(self):
        from modbus_common import decode_registers
        self.dr = decode_registers

    # 1.0 in IEEE-754 big-endian = 0x3F800000
    # Registers in ABCD layout: [0x3F80, 0x0000]
    _ABCD = [0x3F80, 0x0000]
    # CDAB: words swapped   → [0x0000, 0x3F80]
    _CDAB = [0x0000, 0x3F80]
    # BADC: bytes swapped within each word → [0x803F, 0x0000]
    _BADC = [0x803F, 0x0000]
    # DCBA: bytes swapped and words swapped → [0x0000, 0x803F]
    _DCBA = [0x0000, 0x803F]

    def test_abcd_float32(self):
        self.assertAlmostEqual(self.dr(self._ABCD, "float32", "Big",    "Big"),    1.0, places=6)

    def test_cdab_float32(self):
        self.assertAlmostEqual(self.dr(self._CDAB, "float32", "Big",    "Little"), 1.0, places=6)

    def test_badc_float32(self):
        self.assertAlmostEqual(self.dr(self._BADC, "float32", "Little", "Big"),    1.0, places=6)

    def test_dcba_float32(self):
        self.assertAlmostEqual(self.dr(self._DCBA, "float32", "Little", "Little"), 1.0, places=6)

    def test_default_word_order_is_big(self):
        # Calling without word_order should behave as ABCD
        self.assertAlmostEqual(self.dr(self._ABCD, "float32", "Big"), 1.0, places=6)

    def test_word_order_little_reverses_registers_for_int32(self):
        # 0x00010002 = 65538 in big-endian
        # ABCD layout: [0x0001, 0x0002]
        # CDAB layout: [0x0002, 0x0001]
        self.assertEqual(self.dr([0x0001, 0x0002], "int32", "Big", "Big"),    65538)
        self.assertEqual(self.dr([0x0002, 0x0001], "int32", "Big", "Little"), 65538)

    def test_word_order_has_no_effect_on_single_register_types(self):
        # uint16 and bool use only register[0]; word_order is irrelevant
        self.assertEqual(self.dr([0x00FF], "uint16", "Big", "Big"),    255)
        self.assertEqual(self.dr([0x00FF], "uint16", "Big", "Little"), 255)

    def test_endian_little_int16_byte_swapped(self):
        # Register 0x0100 with byte-swap (BADC-style single reg) → actual value 0x0001 = 1
        self.assertEqual(self.dr([0x0100], "int16", "Little", "Big"), 1)

    def test_endian_little_uint16_byte_swapped(self):
        # Regression: uint16 must use the byte-normalized raw, not regs[0] directly.
        # Register 0x0100 big-endian = 256; byte-swapped (Little) = 0x0001 = 1.
        self.assertEqual(self.dr([0x0100], "uint16", "Little", "Big"), 1)
        self.assertEqual(self.dr([0x0100], "uint16", "Big",    "Big"), 256)

    def test_endian_little_uint16_alias(self):
        # UINT PLC alias must honour endian too.
        self.assertEqual(self.dr([0x0100], "UINT", "Little"), 1)


class TestProbeDataType(unittest.TestCase):
    """2.11 — probe pseudo-data-type returns all four layout interpretations."""

    def setUp(self):
        from modbus_common import decode_registers
        self.dr = decode_registers

    def test_probe_returns_dict(self):
        result = self.dr([0x3F80, 0x0000], "probe")
        self.assertIsInstance(result, dict)

    def test_probe_contains_raw_hex(self):
        result = self.dr([0x3F80, 0x0000], "probe")
        self.assertIn("raw_hex", result)
        self.assertEqual(result["raw_hex"], "3f800000")

    def test_probe_contains_raw_registers(self):
        result = self.dr([0x3F80, 0x0000], "probe")
        self.assertEqual(result["raw_registers"], [0x3F80, 0x0000])

    def test_probe_abcd_float32_correct(self):
        result = self.dr([0x3F80, 0x0000], "probe")
        self.assertAlmostEqual(result["ABCD_float32"], 1.0, places=6)

    def test_probe_all_four_layouts_present(self):
        result = self.dr([0x3F80, 0x0000], "probe")
        for label in ("ABCD", "CDAB", "BADC", "DCBA"):
            for typ in ("float32", "int32", "uint32"):
                self.assertIn(f"{label}_{typ}", result, f"{label}_{typ} missing from probe")

    def test_probe_via_parse_response(self):
        from modbus_common import parse_response
        resp = MagicMock()
        resp.registers = [0x3F80, 0x0000]
        result = parse_response("read_holding_registers", resp, {"data_type": "probe"})
        self.assertIsInstance(result, dict)
        self.assertAlmostEqual(result["ABCD_float32"], 1.0, places=6)

    def test_probe_empty_registers_returns_none(self):
        result = self.dr([], "probe")
        self.assertIsNone(result)


class TestRawRegistersDataType(unittest.TestCase):
    """2.12 — raw_registers data type returns uint16 list before any decoding."""

    def setUp(self):
        from modbus_common import decode_registers
        self.dr = decode_registers

    def test_raw_registers_returns_list(self):
        result = self.dr([0x3F80, 0x0000], "raw_registers")
        self.assertEqual(result, [0x3F80, 0x0000])

    def test_raw_registers_masks_to_uint16(self):
        result = self.dr([0x1FFFF], "raw_registers")
        self.assertEqual(result, [0xFFFF])

    def test_raw_registers_single_register(self):
        result = self.dr([42], "raw_registers")
        self.assertEqual(result, [42])

    def test_raw_registers_empty_returns_none(self):
        result = self.dr([], "raw_registers")
        self.assertIsNone(result)

    def test_raw_registers_ignores_endian_and_word_order(self):
        regs = [0x0001, 0x0002]
        self.assertEqual(self.dr(regs, "raw_registers", "Big",    "Big"),    regs)
        self.assertEqual(self.dr(regs, "raw_registers", "Little", "Little"), regs)

    def test_raw_registers_via_parse_response(self):
        from modbus_common import parse_response
        resp = MagicMock()
        resp.registers = [0xABCD, 0x1234]
        result = parse_response("read_holding_registers", resp, {"data_type": "raw_registers"})
        self.assertEqual(result, [0xABCD, 0x1234])


# ---------- normalize_tasks ----------

class TestNormalizeTasks(unittest.TestCase):
    def setUp(self):
        from modbus_common import normalize_tasks
        self.nt = normalize_tasks

    # --- legacy format ---

    def test_legacy_single_connection(self):
        cfg = {
            "connection": {"host": "1.2.3.4", "port": 502},
            "queries": [{"name": "q1", "function": "read_coils"}],
        }
        tasks = self.nt(cfg)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "default")
        self.assertEqual(tasks[0]["connection"]["host"], "1.2.3.4")
        self.assertEqual(tasks[0]["queries"][0]["name"], "q1")

    def test_legacy_missing_queries_raises(self):
        with self.assertRaises(ValueError):
            self.nt({"connection": {}})

    def test_legacy_missing_connection_raises(self):
        with self.assertRaises(ValueError):
            self.nt({"queries": []})

    # --- new tasks format ---

    def test_tasks_format_two_devices(self):
        cfg = {
            "tasks": [
                {"name": "dev_A", "connection": {"host": "10.0.0.1"}, "queries": [{"name": "q1"}]},
                {"name": "dev_B", "connection": {"host": "10.0.0.2"}, "queries": [{"name": "q2"}]},
            ]
        }
        tasks = self.nt(cfg)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["name"], "dev_A")
        self.assertEqual(tasks[1]["name"], "dev_B")

    def test_tasks_format_default_name_assigned(self):
        cfg = {
            "tasks": [
                {"connection": {"host": "10.0.0.1"}, "queries": []},
            ]
        }
        tasks = self.nt(cfg)
        self.assertEqual(tasks[0]["name"], "task_0")

    def test_tasks_empty_list_raises(self):
        with self.assertRaises(ValueError):
            self.nt({"tasks": []})

    def test_tasks_missing_connection_raises(self):
        with self.assertRaises(ValueError):
            self.nt({"tasks": [{"queries": []}]})

    def test_tasks_missing_queries_raises(self):
        with self.assertRaises(ValueError):
            self.nt({"tasks": [{"connection": {}}]})

    def test_tasks_non_dict_entry_raises(self):
        with self.assertRaises(ValueError):
            self.nt({"tasks": ["not_a_dict"]})

    def test_tasks_format_preserves_connection_fields(self):
        cfg = {
            "tasks": [
                {
                    "name": "plc",
                    "connection": {"host": "192.168.1.1", "port": 502, "address_base": 1},
                    "queries": [],
                }
            ]
        }
        tasks = self.nt(cfg)
        self.assertEqual(tasks[0]["connection"]["address_base"], 1)

    # --- tasks key takes precedence over legacy keys ---

    def test_tasks_key_wins_over_legacy(self):
        cfg = {
            "connection": {"host": "legacy"},
            "queries": [],
            "tasks": [{"name": "t", "connection": {"host": "new"}, "queries": []}],
        }
        tasks = self.nt(cfg)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["connection"]["host"], "new")


# ---------- multiview_decode ----------

class TestMultiviewDecode(unittest.TestCase):
    """Tests for the -m / multiview_decode helper."""

    def setUp(self):
        from modbus_common import multiview_decode
        self.mv = multiview_decode

    # --- per-register fields ---

    def test_binary_16_chars_per_register(self):
        result = self.mv([0x3F80, 0x0000], "s", 100)
        self.assertEqual(result["binary"], ["0011111110000000", "0000000000000000"])

    def test_decimal_signed_int16(self):
        # 0xFF00 as signed int16 = -256
        result = self.mv([0xFF00], "s", 0)
        self.assertEqual(result["decimal"], [-256])

    def test_decimal_positive(self):
        result = self.mv([0x0001], "s", 0)
        self.assertEqual(result["decimal"], [1])

    def test_hexadecimal_four_chars(self):
        result = self.mv([0x3F80, 0x0000], "s", 0)
        self.assertEqual(result["hexadecimal"], ["3f80", "0000"])

    def test_name_and_address_in_result(self):
        result = self.mv([0x0000], "temperature", 42)
        self.assertEqual(result["name"], "temperature")
        self.assertEqual(result["address"], 42)

    # --- float32: all four layouts ---
    # 1.0 in IEEE 754 single:
    #   ABCD: bytes 3F 80 00 00 → registers [0x3F80, 0x0000]
    #   CDAB: words swapped     → registers [0x0000, 0x3F80]
    #   BADC: bytes swapped within each word → registers [0x803F, 0x0000]
    #   DCBA: both swapped      → registers [0x0000, 0x803F]

    def test_float32_abcd(self):
        self.assertAlmostEqual(self.mv([0x3F80, 0x0000], "s", 0)["float32_abcd"], 1.0, places=6)

    def test_float32_cdab(self):
        self.assertAlmostEqual(self.mv([0x0000, 0x3F80], "s", 0)["float32_cdab"], 1.0, places=6)

    def test_float32_badc(self):
        self.assertAlmostEqual(self.mv([0x803F, 0x0000], "s", 0)["float32_badc"], 1.0, places=6)

    def test_float32_dcba(self):
        self.assertAlmostEqual(self.mv([0x0000, 0x803F], "s", 0)["float32_dcba"], 1.0, places=6)

    def test_float32_none_when_single_register(self):
        result = self.mv([0x3F80], "s", 0)
        for key in ("float32_abcd", "float32_cdab", "float32_badc", "float32_dcba"):
            self.assertIsNone(result[key], f"{key} should be None with 1 register")

    # --- float64: all four layouts ---
    # 1.0 in IEEE 754 double = 3FF0 0000 0000 0000
    #   ABCD: registers [0x3FF0, 0x0000, 0x0000, 0x0000]
    #   CDAB: word-reversed    [0x0000, 0x0000, 0x0000, 0x3FF0]
    #   BADC: byte-swapped     [0xF03F, 0x0000, 0x0000, 0x0000]
    #   DCBA: both             [0x0000, 0x0000, 0x0000, 0xF03F]

    def test_float64_abcd(self):
        self.assertAlmostEqual(
            self.mv([0x3FF0, 0x0000, 0x0000, 0x0000], "s", 0)["float64_abcd"], 1.0, places=12)

    def test_float64_cdab(self):
        self.assertAlmostEqual(
            self.mv([0x0000, 0x0000, 0x0000, 0x3FF0], "s", 0)["float64_cdab"], 1.0, places=12)

    def test_float64_badc(self):
        self.assertAlmostEqual(
            self.mv([0xF03F, 0x0000, 0x0000, 0x0000], "s", 0)["float64_badc"], 1.0, places=12)

    def test_float64_dcba(self):
        self.assertAlmostEqual(
            self.mv([0x0000, 0x0000, 0x0000, 0xF03F], "s", 0)["float64_dcba"], 1.0, places=12)

    def test_float64_none_when_fewer_than_four_registers(self):
        result = self.mv([0x3F80, 0x0000], "s", 0)
        for key in ("float64_abcd", "float64_cdab", "float64_badc", "float64_dcba"):
            self.assertIsNone(result[key], f"{key} should be None with 2 registers")

    # --- high register values masked to uint16 ---

    def test_high_register_values_masked(self):
        result = self.mv([0x1FFFF], "s", 0)  # > 16 bits
        self.assertEqual(result["binary"], ["1111111111111111"])
        self.assertEqual(result["hexadecimal"], ["ffff"])

    # --- all keys present ---

    def test_all_keys_present_with_four_registers(self):
        result = self.mv([0x3F80, 0x0000, 0x0000, 0x0000], "s", 0)
        for key in ("name", "address", "binary", "decimal", "hexadecimal",
                    "float32_abcd", "float32_cdab", "float32_badc", "float32_dcba",
                    "float64_abcd", "float64_cdab", "float64_badc", "float64_dcba"):
            self.assertIn(key, result, f"key '{key}' missing from multiview result")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
