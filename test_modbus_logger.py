"""
Tests for ModbusClientWrapper methods and execute_query behaviour.
No pytest needed — run: py test_modbus_logger.py
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_wrapper(mock_client):
    """Return a ModbusClientWrapper with a pre-injected mock pymodbus client."""
    with patch("pymodbus.client.ModbusTcpClient", return_value=mock_client):
        from modbus_logger import ModbusClientWrapper
        cfg = {"transport": "tcp", "host": "127.0.0.1", "port": 502}
        wrapper = ModbusClientWrapper(cfg, verbose=False)
        wrapper.client = mock_client
    return wrapper


def _ok_resp():
    r = MagicMock()
    r.isError.return_value = False
    return r


def _err_resp():
    from pymodbus.pdu import ExceptionResponse
    return ExceptionResponse(function_code=1, exception_code=2)


class TestWriteMethods(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.is_socket_open.return_value = True
        self.wrapper = _make_wrapper(self.mock_client)

    def test_write_coil_calls_correct_pymodbus(self):
        self.mock_client.write_coil.return_value = _ok_resp()
        ok, _ = self.wrapper.write_coil(unit=3, address=5, value=True)
        self.assertTrue(ok)
        self.mock_client.write_coil.assert_called_once_with(address=5, value=True, device_id=3)

    def test_write_coils_calls_correct_pymodbus(self):
        self.mock_client.write_coils.return_value = _ok_resp()
        ok, _ = self.wrapper.write_coils(unit=1, address=10, values=[True, False])
        self.assertTrue(ok)
        self.mock_client.write_coils.assert_called_once_with(address=10, values=[True, False], device_id=1)

    def test_write_coil_propagates_error(self):
        self.mock_client.write_coil.return_value = _err_resp()
        ok, msg = self.wrapper.write_coil(unit=1, address=0, value=False)
        self.assertFalse(ok)
        self.assertIn("Modbus exception", msg)

    def test_write_single_register_passes_device_id(self):
        self.mock_client.write_register.return_value = _ok_resp()
        self.wrapper.write_single_register(unit=7, address=100, value=42)
        self.mock_client.write_register.assert_called_once_with(address=100, value=42, device_id=7)

    def test_write_holding_registers_passes_device_id(self):
        self.mock_client.write_registers.return_value = _ok_resp()
        self.wrapper.write_holding_registers(unit=2, address=200, values=[1, 2, 3])
        self.mock_client.write_registers.assert_called_once_with(address=200, values=[1, 2, 3], device_id=2)


class TestDiagMethods(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.is_socket_open.return_value = True
        self.wrapper = _make_wrapper(self.mock_client)

    def test_read_exception_status(self):
        self.mock_client.read_exception_status.return_value = _ok_resp()
        ok, _ = self.wrapper.read_exception_status(unit=1)
        self.assertTrue(ok)
        self.mock_client.read_exception_status.assert_called_once_with(device_id=1)

    def test_diag_query_data(self):
        self.mock_client.diag_query_data.return_value = _ok_resp()
        ok, _ = self.wrapper.diag_query_data(unit=1, msg=b"\x00\x01")
        self.assertTrue(ok)
        self.mock_client.diag_query_data.assert_called_once_with(msg=b"\x00\x01", device_id=1)

    def test_diag_restart_communication(self):
        self.mock_client.diag_restart_communication.return_value = _ok_resp()
        ok, _ = self.wrapper.diag_restart_communication(unit=1, toggle=True)
        self.assertTrue(ok)
        self.mock_client.diag_restart_communication.assert_called_once_with(toggle=True, device_id=1)

    def test_read_diagnostic_register(self):
        self.mock_client.diag_read_diagnostic_register.return_value = _ok_resp()
        ok, _ = self.wrapper.read_diagnostic_register(unit=1)
        self.assertTrue(ok)
        self.mock_client.diag_read_diagnostic_register.assert_called_once_with(device_id=1)


class TestDeviceIdMethods(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.is_socket_open.return_value = True
        self.wrapper = _make_wrapper(self.mock_client)

    def test_read_device_identification_calls_report_device_id(self):
        self.mock_client.report_device_id.return_value = _ok_resp()
        ok, _ = self.wrapper.read_device_identification(unit=4)
        self.assertTrue(ok)
        self.mock_client.report_device_id.assert_called_once_with(device_id=4)

    def test_read_device_identification_handles_exception(self):
        self.mock_client.report_device_id.side_effect = RuntimeError("comm error")
        ok, msg = self.wrapper.read_device_identification(unit=1)
        self.assertFalse(ok)
        self.assertIn("Exception", msg)

    def test_read_device_information_calls_mei(self):
        self.mock_client.read_device_information.return_value = _ok_resp()
        ok, _ = self.wrapper.read_device_information(unit=1)
        self.assertTrue(ok)
        self.mock_client.read_device_information.assert_called_once_with(device_id=1)

    def test_mask_write_register_passes_device_id(self):
        self.mock_client.mask_write_register.return_value = _ok_resp()
        ok, _ = self.wrapper.mask_write_register(unit=5, address=300, and_mask=0xFF00, or_mask=0x00FF)
        self.assertTrue(ok)
        self.mock_client.mask_write_register.assert_called_once_with(
            address=300, and_mask=0xFF00, or_mask=0x00FF, device_id=5
        )


class TestCallMap(unittest.TestCase):
    def test_all_new_functions_in_call_map(self):
        from modbus_logger import _CALL_MAP
        for func in ("write_coil", "write_coils", "read_exception_status",
                     "read_diagnostic_register", "diag_query_data",
                     "diag_restart_communication", "read_device_identification",
                     "read_device_information"):
            self.assertIn(func, _CALL_MAP, f"{func} missing from _CALL_MAP")

    def test_read_device_identification_not_redirected(self):
        from modbus_logger import _CALL_MAP
        method, _ = _CALL_MAP["read_device_identification"]
        self.assertEqual(method, "read_device_identification",
                         "read_device_identification should call its own method, not redirect to read_device_information")

    def test_new_read_funcs_in_store_funcs(self):
        from modbus_logger import _STORE_FUNCS
        for func in ("read_exception_status", "read_diagnostic_register",
                     "read_device_identification", "read_device_information"):
            self.assertIn(func, _STORE_FUNCS, f"{func} missing from _STORE_FUNCS")


class TestExecuteQueryVerbose(unittest.TestCase):
    """execute_query must respect the verbose flag captured at function entry."""

    def _run_query(self, verbose: bool):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.isError.return_value = False
        resp.registers = [42]
        mock_client.read_holding_registers.return_value = resp

        db = MagicMock()

        cfg = {
            "verbose": verbose,
            "save_audit": False,
        }
        query = {
            "name": "test_reg",
            "function": "read_holding_registers",
            "unit": 1,
            "address": 0,
            "count": 1,
            "data_type": "uint16",
            "endian": "Big",
        }

        wrapper = _make_wrapper(mock_client)

        from modbus_logger import execute_query
        import io
        captured = io.StringIO()
        import sys as _sys
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            execute_query(wrapper, db, query, cfg, address_base=0)
        finally:
            _sys.stdout = old_stdout
        return captured.getvalue()

    def test_verbose_true_produces_output(self):
        output = self._run_query(verbose=True)
        self.assertTrue(len(output) > 0, "verbose=True should produce log output")

    def test_verbose_false_produces_no_output(self):
        output = self._run_query(verbose=False)
        self.assertEqual(output, "", "verbose=False must produce no output on success")

    def test_failure_silent_when_not_verbose(self):
        mock_client = MagicMock()
        from pymodbus.pdu import ExceptionResponse
        mock_client.read_holding_registers.return_value = ExceptionResponse(3, 2)

        db = MagicMock()
        cfg = {"verbose": False, "save_audit": False}
        query = {"name": "t", "function": "read_holding_registers",
                 "unit": 1, "address": 0, "count": 1}

        wrapper = _make_wrapper(mock_client)

        from modbus_logger import execute_query
        import io, sys as _sys
        captured = io.StringIO()
        old_stdout = _sys.stdout
        _sys.stdout = captured
        try:
            execute_query(wrapper, db, query, cfg, address_base=0)
        finally:
            _sys.stdout = old_stdout
        self.assertEqual(captured.getvalue(), "",
                         "verbose=False must suppress failure log too")


class TestAdvancedDiagMethods(unittest.TestCase):
    """All no-arg diag counter methods follow the same pattern; one test each."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.is_socket_open.return_value = True
        self.wrapper = _make_wrapper(self.mock_client)

    def _ok(self):
        r = MagicMock()
        r.isError.return_value = False
        return r

    def _test_no_arg(self, method_name, client_attr=None):
        client_attr = client_attr or method_name
        getattr(self.mock_client, client_attr).return_value = self._ok()
        ok, _ = getattr(self.wrapper, method_name)(unit=1)
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
        self.mock_client.diag_change_ascii_input_delimeter.return_value = self._ok()
        ok, _ = self.wrapper.diag_change_ascii_input_delimeter(unit=1, data=0x0D)
        self.assertTrue(ok)
        self.mock_client.diag_change_ascii_input_delimeter.assert_called_once_with(data=0x0D, device_id=1)

    def test_read_fifo_queue(self):
        self.mock_client.read_fifo_queue.return_value = self._ok()
        ok, _ = self.wrapper.read_fifo_queue(unit=1, queue_register_address=100)
        self.assertTrue(ok)
        self.mock_client.read_fifo_queue.assert_called_once_with(address=100, device_id=1)

    def test_read_file_record(self):
        self.mock_client.read_file_record.return_value = self._ok()
        ok, _ = self.wrapper.read_file_record(unit=1, file_record=[(4, 1, 2)])
        self.assertTrue(ok)
        self.mock_client.read_file_record.assert_called_once_with(file_record=[(4, 1, 2)], device_id=1)

    def test_write_file_record(self):
        self.mock_client.write_file_record.return_value = self._ok()
        ok, _ = self.wrapper.write_file_record(unit=1, file_record=[(4, 1, [0x1234])])
        self.assertTrue(ok)
        self.mock_client.write_file_record.assert_called_once_with(file_record=[(4, 1, [0x1234])], device_id=1)

    def test_readwrite_registers(self):
        self.mock_client.readwrite_registers.return_value = self._ok()
        ok, _ = self.wrapper.readwrite_registers(
            unit=1, read_address=10, read_count=2, write_address=20, write_registers=[1, 2])
        self.assertTrue(ok)
        self.mock_client.readwrite_registers.assert_called_once_with(
            read_address=10, read_count=2, write_address=20, write_registers=[1, 2], device_id=1)

    def test_exception_propagated_as_false(self):
        self.mock_client.diag_force_listen_only.side_effect = RuntimeError("no such function")
        ok, msg = self.wrapper.diag_force_listen_only(unit=1)
        self.assertFalse(ok)
        self.assertIn("Exception", msg)


class TestAdvancedCallMap(unittest.TestCase):
    def test_all_advanced_functions_in_call_map(self):
        from modbus_logger import _CALL_MAP
        for func in (
            "diag_force_listen_only", "diag_clear_counters",
            "diag_read_bus_message_count", "diag_read_bus_comm_error_count",
            "diag_read_bus_exception_error_count", "diag_read_device_message_count",
            "diag_read_device_no_response_count", "diag_read_device_nak_count",
            "diag_read_device_busy_count", "diag_read_bus_char_overrun_count",
            "diag_read_iop_overrun_count", "diag_clear_overrun_counter",
            "diag_getclear_modbus_response", "diag_get_comm_event_counter",
            "diag_get_comm_event_log", "diag_change_ascii_input_delimeter",
            "read_file_record", "write_file_record",
            "readwrite_registers", "read_fifo_queue",
        ):
            self.assertIn(func, _CALL_MAP, f"{func} missing from _CALL_MAP")

    def test_counter_funcs_in_store_funcs(self):
        from modbus_logger import _STORE_FUNCS
        for func in (
            "diag_read_bus_message_count", "diag_read_bus_comm_error_count",
            "diag_read_bus_exception_error_count", "diag_read_device_message_count",
            "diag_read_device_no_response_count", "diag_read_device_nak_count",
            "diag_read_device_busy_count", "diag_read_bus_char_overrun_count",
            "diag_read_iop_overrun_count", "diag_get_comm_event_counter",
            "diag_get_comm_event_log", "read_fifo_queue",
            "readwrite_registers", "read_file_record",
        ):
            self.assertIn(func, _STORE_FUNCS, f"{func} missing from _STORE_FUNCS")

    def test_readwrite_registers_required_args(self):
        from modbus_logger import _CALL_MAP
        _, args = _CALL_MAP["readwrite_registers"]
        self.assertEqual(set(args), {"read_address", "read_count", "write_address", "write_registers"})

    def test_read_fifo_queue_required_args(self):
        from modbus_logger import _CALL_MAP
        _, args = _CALL_MAP["read_fifo_queue"]
        self.assertIn("queue_register_address", args)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
