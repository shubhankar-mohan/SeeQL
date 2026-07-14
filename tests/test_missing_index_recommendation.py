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
