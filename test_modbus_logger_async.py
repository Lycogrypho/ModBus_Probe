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
        conn = {"host": "127.0.0.1", "port": 502}
        wrapper = _mod.ModbusLoggerAsync(conn, verbose=False)
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


class TestAsyncAdvancedDiagMethods(unittest.TestCase):
    """All new async diag/file/register methods follow the same pattern."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.wrapper = _make_async_wrapper(self.mock_client)

    def _ok(self):
        r = MagicMock()
        r.isError.return_value = False
        return r

    def _test_no_arg(self, method_name, client_attr=None):
        client_attr = client_attr or method_name
        getattr(self.mock_client, client_attr).return_value = self._ok()
        setattr(self.mock_client, client_attr, AsyncMock(return_value=self._ok()))
        ok, _ = _run(getattr(self.wrapper, method_name)(unit=1))
        self.assertTrue(ok)
        getattr(self.mock_client, client_attr).assert_called_once_with(device_id=1)

    def test_diag_force_listen_only(self):
        self._test_no_arg("diag_force_listen_only")

    def test_diag_clear_counters(self):
        self._test_no_arg("diag_clear_counters")

    def test_diag_read_bus_message_count(self):
        self._test_no_arg("diag_read_bus_message_count")

    def test_diag_read_bus_comm_error_count(self):
        self._test_no_arg("diag_read_bus_comm_error_count")

    def test_diag_read_bus_exception_error_count(self):
        self._test_no_arg("diag_read_bus_exception_error_count")

    def test_diag_read_device_message_count(self):
        self._test_no_arg("diag_read_device_message_count")

    def test_diag_read_device_no_response_count(self):
        self._test_no_arg("diag_read_device_no_response_count")

    def test_diag_read_device_nak_count(self):
        self._test_no_arg("diag_read_device_nak_count")

    def test_diag_read_device_busy_count(self):
        self._test_no_arg("diag_read_device_busy_count")

    def test_diag_read_bus_char_overrun_count(self):
        self._test_no_arg("diag_read_bus_char_overrun_count")

    def test_diag_read_iop_overrun_count(self):
        self._test_no_arg("diag_read_iop_overrun_count")

    def test_diag_clear_overrun_counter(self):
        self._test_no_arg("diag_clear_overrun_counter")

    def test_diag_getclear_modbus_response(self):
        self._test_no_arg("diag_getclear_modbus_response")

    def test_diag_get_comm_event_counter(self):
        self._test_no_arg("diag_get_comm_event_counter")

    def test_diag_get_comm_event_log(self):
        self._test_no_arg("diag_get_comm_event_log")

    def test_diag_change_ascii_input_delimeter(self):
        self.mock_client.diag_change_ascii_input_delimeter = AsyncMock(return_value=self._ok())
        ok, _ = _run(self.wrapper.diag_change_ascii_input_delimeter(unit=1, data=0x0D))
        self.assertTrue(ok)
        self.mock_client.diag_change_ascii_input_delimeter.assert_called_once_with(data=0x0D, device_id=1)

    def test_read_fifo_queue(self):
        self.mock_client.read_fifo_queue = AsyncMock(return_value=self._ok())
        ok, _ = _run(self.wrapper.read_fifo_queue(unit=1, queue_register_address=100))
        self.assertTrue(ok)
        self.mock_client.read_fifo_queue.assert_called_once_with(address=100, device_id=1)

    def test_read_file_record(self):
        self.mock_client.read_file_record = AsyncMock(return_value=self._ok())
        ok, _ = _run(self.wrapper.read_file_record(unit=1, file_record=[(4, 1, 2)]))
        self.assertTrue(ok)
        self.mock_client.read_file_record.assert_called_once_with(file_record=[(4, 1, 2)], device_id=1)

    def test_write_file_record(self):
        self.mock_client.write_file_record = AsyncMock(return_value=self._ok())
        ok, _ = _run(self.wrapper.write_file_record(unit=1, file_record=[(4, 1, [0x1234])]))
        self.assertTrue(ok)
        self.mock_client.write_file_record.assert_called_once_with(file_record=[(4, 1, [0x1234])], device_id=1)

    def test_readwrite_registers(self):
        self.mock_client.readwrite_registers = AsyncMock(return_value=self._ok())
        ok, _ = _run(self.wrapper.readwrite_registers(
            unit=1, read_address=10, read_count=2, write_address=20, write_registers=[1, 2]))
        self.assertTrue(ok)
        self.mock_client.readwrite_registers.assert_called_once_with(
            read_address=10, read_count=2, write_address=20, write_registers=[1, 2], device_id=1)

    def test_exception_propagated_as_false(self):
        self.mock_client.diag_force_listen_only = AsyncMock(side_effect=RuntimeError("no such function"))
        ok, msg = _run(self.wrapper.diag_force_listen_only(unit=1))
        self.assertFalse(ok)
        self.assertIn("Exception", msg)


class TestAsyncMultiTaskDispatch(unittest.TestCase):
    """Async execute_query is invoked once per query for each task."""

    def _make_client_with_registers(self, reg_value):
        mc = MagicMock()
        resp = MagicMock()
        resp.isError.return_value = False
        resp.registers = [reg_value]
        mc.read_holding_registers = AsyncMock(return_value=resp)
        return _make_async_wrapper(mc), mc

    def test_two_tasks_each_query_executed(self):
        import modbus_logger_async as _mod
        wrapper_a, mc_a = self._make_client_with_registers(10)
        wrapper_b, mc_b = self._make_client_with_registers(20)
        db = MagicMock()
        cfg = {"verbose": False, "save_audit": False}
        q = {"name": "reg", "function": "read_holding_registers",
             "unit": 1, "address": 0, "count": 1, "data_type": "uint16", "endian": "Big"}

        async def run():
            await _mod.execute_query(wrapper_a, db, q, cfg, address_base=0)
            await _mod.execute_query(wrapper_b, db, q, cfg, address_base=0)

        _run(run())
        mc_a.read_holding_registers.assert_called_once()
        mc_b.read_holding_registers.assert_called_once()
        self.assertEqual(db.insert_data.call_count, 2)

    def test_address_base_per_task(self):
        import modbus_logger_async as _mod
        wrapper, mc = self._make_client_with_registers(0)
        db = MagicMock()
        cfg = {"verbose": False, "save_audit": False}
        q = {"name": "r", "function": "read_holding_registers",
             "unit": 1, "address": 1, "count": 1}

        _run(_mod.execute_query(wrapper, db, q, cfg, address_base=1))
        mc.read_holding_registers.assert_called_once_with(address=0, count=1, device_id=1)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
