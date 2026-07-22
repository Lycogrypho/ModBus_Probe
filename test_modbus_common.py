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


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
