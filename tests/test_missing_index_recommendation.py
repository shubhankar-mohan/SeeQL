from alerting.correlators import missing_index as mi


def test_recommend_never_returns_drop_for_scan():
    redundant = [{"sql_drop_index": "ALTER TABLE `db`.`t` DROP INDEX `idx_x`"}]
    rec = mi._recommend_index(
        "db", "t",
        {"digest_text": "SELECT `user_id` FROM `t` WHERE `phone` = ? AND `is_active` = ?"},
        [], redundant,
    )
    assert rec is None or rec.upper().lstrip().startswith(("CREATE", "ALTER TABLE `DB`.`T` ADD"))
    assert rec is None or "DROP INDEX" not in rec.upper()


def test_predicate_derived_create_index():
    rec = mi._recommend_index(
        "db", "t",
        {"digest_text": "SELECT `user_id` FROM `t` WHERE `phone` = ? AND `is_active` = ?"},
        [], [],
    )
    # Either a CREATE/ADD INDEX on the predicate columns, or None (deferred to LLM)
    if rec is not None:
        assert "phone" in rec.lower()
        assert "drop" not in rec.lower()


def test_join_without_where_does_not_yield_garbage_join_column():
    # No WHERE clause at all -- the only predicate is the JOIN ... ON
    # condition. The old `_PREDICATE_RE` matched "IN" inside the keyword
    # "JOIN" itself (backtracking \w+ down to "JO"), and the old scope
    # narrowing only recognized " where ", so it fell back to scanning the
    # entire digest text -- including the SELECT-list projection column
    # `a`.`user_id`.
    rec = mi._recommend_index(
        "db", "a",
        {
            "digest_text": (
                "SELECT `a`.`user_id` FROM `a` JOIN `b` "
                "ON `a`.`phone` = `b`.`phone`"
            )
        },
        [], [],
    )
    assert rec is not None
    lowered = rec.lower()
    assert "phone" in lowered
    # No bogus column pulled out of the keyword "JOIN" itself.
    assert "`jo`" not in lowered
    assert "`join`" not in lowered
    # No SELECT-list projection column leaked in as a predicate.
    assert "user_id" not in lowered
    assert "drop" not in lowered
