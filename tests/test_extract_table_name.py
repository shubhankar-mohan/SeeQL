from agent.state_builder import _extract_table_name


def test_schema_qualified_backticked():
    assert _extract_table_name("SELECT * FROM `appdb`.`order_reconciliation` WHERE x=1") == "order_reconciliation"

def test_schema_qualified_bare():
    assert _extract_table_name("SELECT * FROM appdb.orders o JOIN x") == "orders"

def test_plain_table():
    assert _extract_table_name("UPDATE crews SET a=1") == "crews"

def test_backticked_plain():
    assert _extract_table_name("SELECT * FROM `pirates`") == "pirates"

def test_no_table():
    assert _extract_table_name("SHOW STATUS") == "?"
