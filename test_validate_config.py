"""
Tests for modbus_common.validate_tasks.
No pytest needed — run: py test_validate_config.py
"""
# OopCompanion:suppressRename
import unittest
from modbus_common import validate_tasks


def _task(name="t", conn=None, queries=None):
    """Build a minimal valid task dict."""
    return {
        "name": name,
        "connection": conn or {"transport": "tcp", "host": "127.0.0.1", "port": 502},
        "queries": queries if queries is not None else [_q()],
    }


def _q(name="sensor", func="read_holding_registers", extra=None):
    """Build a minimal valid query dict."""
    q = {"name": name, "function": func, "unit": 1, "address": 100, "count": 2}
    if extra:
        q.update(extra)
    return q


class TestValidTasksPassesClean(unittest.TestCase):
    def test_valid_single_task(self):
        self.assertEqual(validate_tasks([_task()]), [])

    def test_valid_two_tasks(self):
        tasks = [_task("a"), _task("b", queries=[_q("b_sensor")])]
        self.assertEqual(validate_tasks(tasks), [])

    def test_valid_coil_query(self):
        q = {"name": "coil1", "function": "read_coils", "unit": 1, "address": 0, "count": 1}
        self.assertEqual(validate_tasks([_task(queries=[q])]), [])

    def test_valid_write_coil_query(self):
        q = {"name": "w1", "function": "write_coil", "unit": 1, "address": 5, "value": True}
        self.assertEqual(validate_tasks([_task(queries=[q])]), [])

    def test_empty_task_list_passes(self):
        self.assertEqual(validate_tasks([]), [])


class TestConnectionValidation(unittest.TestCase):
    def test_invalid_transport(self):
        conn = {"transport": "udp", "host": "127.0.0.1", "port": 502}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("transport" in e for e in errs))

    def test_empty_host(self):
        conn = {"transport": "tcp", "host": "", "port": 502}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("host" in e for e in errs))

    def test_non_string_host(self):
        conn = {"transport": "tcp", "host": 12345, "port": 502}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("host" in e for e in errs))

    def test_port_zero_invalid(self):
        conn = {"transport": "tcp", "host": "127.0.0.1", "port": 0}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("port" in e for e in errs))

    def test_port_too_large_invalid(self):
        conn = {"transport": "tcp", "host": "127.0.0.1", "port": 99999}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("port" in e for e in errs))

    def test_port_non_numeric_invalid(self):
        conn = {"transport": "tcp", "host": "127.0.0.1", "port": "abc"}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("port" in e for e in errs))

    def test_negative_timeout_invalid(self):
        conn = {"transport": "tcp", "host": "127.0.0.1", "port": 502, "timeout": -1}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("timeout" in e for e in errs))

    def test_zero_timeout_invalid(self):
        conn = {"transport": "tcp", "host": "127.0.0.1", "port": 502, "timeout": 0}
        errs = validate_tasks([_task(conn=conn)])
        self.assertTrue(any("timeout" in e for e in errs))

    def test_serial_transport_skips_host_port_check(self):
        conn = {"transport": "serial", "port": "COM3"}
        errs = validate_tasks([_task(conn=conn)])
        self.assertEqual(errs, [])


class TestQueryValidation(unittest.TestCase):
    def test_empty_queries_list(self):
        errs = validate_tasks([_task(queries=[])])
        self.assertTrue(any("queries" in e for e in errs))

    def test_missing_query_name(self):
        q = {"function": "read_holding_registers", "unit": 1, "address": 0, "count": 1}
        errs = validate_tasks([_task(queries=[q])])
        self.assertTrue(any("name" in e for e in errs))

    def test_missing_function(self):
        q = {"name": "x", "unit": 1, "address": 0, "count": 1}
        errs = validate_tasks([_task(queries=[q])])
        self.assertTrue(any("function" in e for e in errs))

    def test_unknown_function(self):
        q = _q(func="read_flying_registers")
        errs = validate_tasks([_task(queries=[q])])
        self.assertTrue(any("unknown function" in e for e in errs))

    def test_missing_required_arg_address(self):
        q = {"name": "x", "function": "read_holding_registers", "unit": 1, "count": 2}
        errs = validate_tasks([_task(queries=[q])])
        self.assertTrue(any("address" in e for e in errs))

    def test_missing_required_arg_count(self):
        q = {"name": "x", "function": "read_coils", "unit": 1, "address": 0}
        errs = validate_tasks([_task(queries=[q])])
        self.assertTrue(any("count" in e for e in errs))

    def test_duplicate_query_name_across_tasks(self):
        tasks = [
            _task("a", queries=[_q("same")]),
            _task("b", queries=[_q("same")]),
        ]
        errs = validate_tasks(tasks)
        self.assertTrue(any("duplicate" in e.lower() for e in errs))

    def test_no_required_args_function_passes(self):
        q = {"name": "exc", "function": "read_exception_status", "unit": 1}
        self.assertEqual(validate_tasks([_task(queries=[q])]), [])

    def test_multiple_errors_returned(self):
        conn = {"transport": "ftp", "host": "", "port": "x"}
        q = {"function": "not_a_function"}
        errs = validate_tasks([{"name": "bad", "connection": conn, "queries": [q]}])
        self.assertGreater(len(errs), 1)


if __name__ == "__main__":
    unittest.main()
