from collapse import collapse_repeats


def test_timestamped_repeat_is_collapsed():
    lines = ["FAIL test_3", "x", "FAIL test_4", "x", "12:00:01 FAIL test_3"]
    assert collapse_repeats(lines, [1, 3, 5]) == [(1, 5, 2), (3, 3, 1)]
