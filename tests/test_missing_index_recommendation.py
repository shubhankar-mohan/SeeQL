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


def test_equality_before_range_column_ordering():
    # P3-6: `amount` (range, `>`) appears before `active` (equality, `=`) in
    # the WHERE clause's textual/source order, but a composite index must
    # put the equality column first -- a range column ahead of an equality
    # column in `ADD INDEX (...)` makes the equality column unusable for a
    # seek. Recommendation must reorder to (active, amount).
    rec = mi._recommend_index(
        "db", "t",
        {"digest_text": "SELECT `user_id` FROM `t` WHERE `amount` > ? AND `active` = ?"},
        [], [],
    )
    assert rec is not None
    assert "(`active`, `amount`)" in rec


def test_all_equality_predicates_preserve_source_order():
    # When every predicate is an equality predicate, there's no reordering
    # to do -- source (textual) order is preserved.
    rec = mi._recommend_index(
        "db", "t",
        {"digest_text": "SELECT `user_id` FROM `t` WHERE `phone` = ? AND `is_active` = ?"},
        [], [],
    )
    assert rec is not None
    assert "(`phone`, `is_active`)" in rec


def test_equality_range_and_order_by_column_ordering():
    # Equality columns first, then range columns, then ORDER BY (sort)
    # columns -- even though `amount` (range) precedes `active` (equality)
    # textually, and `created_at` (sort) is a separate, later clause.
    rec = mi._recommend_index(
        "db", "t",
        {
            "digest_text": (
                "SELECT `user_id` FROM `t` WHERE `amount` > ? AND `active` = ? "
                "ORDER BY `created_at`"
            )
        },
        [], [],
    )
    assert rec is not None
    assert "(`active`, `amount`, `created_at`)" in rec


def test_order_by_function_call_is_not_indexed():
    # `ORDER BY DATE(`created_at`)` is a function call, not a bare column.
    # It must be SKIPPED -- never fabricated into invalid DDL with nested,
    # unescaped backticks like `idx_active_DATE(`created_at`)`. Only the
    # WHERE-predicate column survives.
    rec = mi._recommend_index(
        "db", "t",
        {
            "digest_text": (
                "SELECT `user_id` FROM `t` WHERE `active` = ? "
                "ORDER BY DATE(`created_at`) DESC"
            )
        },
        [], [],
    )
    assert rec is not None
    assert "(`active`)" in rec
    # No fragment of the function-call expression leaked into the DDL.
    assert "date(" not in rec.lower()
    assert "created_at" not in rec.lower()
    # DDL stays well-formed: exactly one backtick pair per column, no nesting.
    assert rec.count("(`") == 1


def test_order_by_positional_reference_is_not_indexed():
    # `ORDER BY 2` is a positional reference to the 2nd select-list column,
    # NOT a column literally named "2". It must be skipped -- we can't resolve
    # a position to a column name here, and indexing a column called "2" is
    # nonsense. Only the WHERE-predicate column survives.
    rec = mi._recommend_index(
        "db", "t",
        {"digest_text": "SELECT `user_id`, `created_at` FROM `t` WHERE `active` = ? ORDER BY 2"},
        [], [],
    )
    assert rec is not None
    assert "(`active`)" in rec
    assert "`2`" not in rec


def test_order_by_bare_column_is_appended():
    # The positive control for the guard: a genuine bare column in ORDER BY
    # (backtick-quoted, no function/expression) IS appended last, after the
    # equality predicate column.
    rec = mi._recommend_index(
        "db", "t",
        {"digest_text": "SELECT `user_id` FROM `t` WHERE `active` = ? ORDER BY `created_at`"},
        [], [],
    )
    assert rec is not None
    assert "(`active`, `created_at`)" in rec
