from factory.harness.test_quality import assess_tests, extract_tests, path_regex
from factory.spec import load_spec

from .conftest import FIXTURES, SPECS

V1 = FIXTURES / "scripted" / "orders_v1" / "files"


def test_good_suite_passes_static_gate() -> None:
    spec = load_spec(SPECS / "orders.yaml")
    result = assess_tests({"tests/test_orders.py": (V1 / "test_orders.py").read_text()}, spec)
    assert result.passed, result.failures


def test_weak_suite_lacks_varied_inputs() -> None:
    spec = load_spec(SPECS / "orders.yaml")
    result = assess_tests({"tests/test_orders.py": (V1 / "test_orders_weak.py").read_text()},
                          spec)
    assert not result.passed
    assert result.failures == [
        "no test with varied inputs (parametrize, or several writes in one test)"]


def test_missing_operation_and_stateful_test_are_reported() -> None:
    spec = load_spec(SPECS / "orders.yaml")
    source = (
        "import pytest\n"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_create(client, n):\n"
        "    client.post('/orders', json={})\n"
        "def test_list(client):\n"
        "    client.get('/orders')\n"
    )
    failures = assess_tests({"tests/test_orders.py": source}, spec).failures
    assert "no test calls GET /orders/{id} (getOrder)" in failures
    assert any("no stateful test" in f for f in failures)


def test_syntax_errors_are_reported() -> None:
    spec = load_spec(SPECS / "orders.yaml")
    result = assess_tests({"tests/test_orders.py": "def test_(:\n"}, spec)
    assert not result.passed
    assert "syntax error" in result.failures[0]


def test_extract_tests_understands_fstrings_and_queries() -> None:
    source = (
        "def test_a(client):\n"
        "    oid = 1\n"
        "    client.post('/orders', json={})\n"
        "    client.get(f'/orders/{oid}')\n"
        "    client.get('/orders?status=paid')\n"
        "    client.get('/orders/' + str(oid))\n"
    )
    [fn] = extract_tests(source)
    assert fn.calls == [("post", "/orders"), ("get", "/orders/{x}"), ("get", "/orders"),
                        ("get", "/orders/{x}")]


def test_path_regex() -> None:
    rx = path_regex("/orders/{id}/cancel")
    assert rx.match("/orders/{x}/cancel")
    assert rx.match("/orders/12/cancel")
    assert not rx.match("/orders/12")
