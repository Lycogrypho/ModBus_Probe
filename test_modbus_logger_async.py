"""
Tests for ModbusLoggerAsync methods and async execute_query behaviour.
No pytest needed — run: py test_modbus_logger_async.py
"""
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_async_wrapper(mock_client):
    """Return a ModbusLoggerAsync with a pre-injected mock async pymodbus client."""
    import modbus_logger_async as _mod
    with patch.object(_mod, "AsyncModbusTcpClient", return_value=mock_client):
        cfg = {"connection": {"host": "127.0.0.1", "port": 502}}
        wrapper = _mod.ModbusLoggerAsync(cfg, verbose=False)
        wrapper.client = mock_client
    return wrapper


def _ok_resp():
    r = MagicMock()
    r.isError.return_value = False
    return r


def _run(coro):
    return asyncio.run(coro)


class TestAsyncWriteMethods(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.write_coil = AsyncMock(return_value=_ok_resp())
        self.mock_client.write_coils = AsyncMock(return_value=_ok_resp())
        self.wrapper = _make_async_wrapper(self.mock_client)

    def test_write_coil_calls_correct_pymodbus(self):
        ok, _ = _run(self.wrapper.write_coil(unit=3, address=5, value=True))
        self.assertTrue(ok)
        self.mock_client.write_coil.assert_called_once_with(address=5, value=True, device_id=3)

    def test_write_coils_calls_correct_pymodbus(self):
        ok, _ = _run(self.wrapper.write_coils(unit=1, address=10, values=[True, False]))
        self.assertTrue(ok)
        self.mock_client.write_coils.assert_called_once_with(address=10, values=[True, False], device_id=1)


class TestAsyncDiagMethods(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.read_exception_status = AsyncMock(return_value=_ok_resp())
        self.mock_client.diag_query_data = AsyncMock(return_value=_ok_resp())
        self.mock_client.diag_restart_communication = AsyncMock(return_value=_ok_resp())
        self.mock_client.diag_read_diagnostic_register = AsyncMock(return_value=_ok_resp())
        self.wrapper = _make_async_wrapper(self.mock_client)

    def test_read_exception_status(self):
        ok, _ = _run(self.wrapper.read_exception_status(unit=1))
        self.assertTrue(ok)
        self.mock_client.read_exception_status.assert_called_once_with(device_id=1)

    def test_diag_query_data(self):
        ok, _ = _run(self.wrapper.diag_query_data(unit=1, msg=b"\x00\x01"))
        self.assertTrue(ok)
        self.mock_client.diag_query_data.assert_called_once_with(msg=b"\x00\x01", device_id=1)

    def test_diag_restart_communication(self):
        ok, _ = _run(self.wrapper.diag_restart_communication(unit=1, toggle=True))
        self.assertTrue(ok)
        self.mock_client.diag_restart_communication.assert_called_once_with(toggle=True, device_id=1)

    def test_read_diagnostic_register(self):
        ok, _ = _run(self.wrapper.read_diagnostic_register(unit=1))
        self.assertTrue(ok)
        self.mock_client.diag_read_diagnostic_register.assert_called_once_with(device_id=1)


class TestAsyncDeviceIdMethods(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.report_device_id = AsyncMock(return_value=_ok_resp())
        self.mock_client.read_device_information = AsyncMock(return_value=_ok_resp())
        self.wrapper = _make_async_wrapper(self.mock_client)

    def test_read_device_identification_calls_report_device_id(self):
        ok, _ = _run(self.wrapper.read_device_identification(unit=4))
        self.assertTrue(ok)
        self.mock_client.report_device_id.assert_called_once_with(device_id=4)

    def test_read_device_identification_handles_exception(self):
        self.mock_client.report_device_id.side_effect = RuntimeError("comm error")
        ok, msg = _run(self.wrapper.read_device_identification(unit=1))
        self.assertFalse(ok)
        self.assertIn("Exception", msg)

    def test_read_device_information_calls_mei(self):
        ok, _ = _run(self.wrapper.read_device_information(unit=1))
        self.assertTrue(ok)
        self.mock_client.read_device_information.assert_called_once_with(device_id=1)


class TestModbusLoggerAsync(unittest.TestCase):
    def test_all_new_functions_in_call_map(self):
        from modbus_logger_async import CALL_MAP
        for func in ("write_coil", "write_coils", "read_exception_status",
                     "read_diagnostic_register", "diag_query_data",
                     "diag_restart_communication", "read_device_identification",
                     "read_device_information"):
            self.assertIn(func, CALL_MAP, f"{func} missing from CALL_MAP")

    def test_read_device_identification_not_redirected(self):
        from modbus_logger_async import CALL_MAP
        method, _ = CALL_MAP["read_device_identification"]
        self.assertEqual(method, "read_device_identification",
                         "read_device_identification must map to its own method, not read_device_information")

    def test_new_read_funcs_in_store_funcs(self):
        from modbus_logger_async import STORE_FUNCS
        for func in ("read_exception_status", "read_diagnostic_register",
                     "read_device_identification", "read_device_information"):
            self.assertIn(func, STORE_FUNCS, f"{func} missing from STORE_FUNCS")

    def test_call_map_matches_sync(self):
        from modbus_logger import _CALL_MAP
        from modbus_logger_async import CALL_MAP
        self.assertEqual(set(_CALL_MAP.keys()), set(CALL_MAP.keys()),
                         "async CALL_MAP keys must match sync _CALL_MAP keys")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
