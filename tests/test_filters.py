from rss_ticker.filters import FilterRule, evaluate, highlights, include_patterns


def test_no_rules_includes_everything():
    assert evaluate([], "Anything", None) == (True, False)


def test_only_highlight_rules_still_includes_everything():
    rules = [FilterRule("nvidia", "highlight")]
    assert evaluate(rules, "Apple ships", None) == (True, False)
    assert evaluate(rules, "Nvidia beats", None) == (True, True)


def test_include_rule_excludes_non_matching():
    rules = [FilterRule("fed", "include")]
    assert evaluate(rules, "Fed holds rates", None) == (True, False)
    assert evaluate(rules, "Oil slips", None) == (False, False)


def test_any_include_rule_matching_is_enough():
    rules = [FilterRule("fed", "include"), FilterRule("oil", "include")]
    assert evaluate(rules, "Oil slips", None)[0] is True


def test_matching_is_case_insensitive():
    assert evaluate([FilterRule("NVIDIA", "highlight")], "nvidia up", None) == (True, True)


def test_summary_is_searched_too():
    rules = [FilterRule("earnings", "include")]
    assert evaluate(rules, "Chipmaker update", "Quarterly earnings beat")[0] is True


def test_missing_summary_does_not_crash():
    assert evaluate([FilterRule("x", "include")], "no match", None) == (False, False)


def test_include_and_highlight_combine():
    rules = [FilterRule("fed", "include"), FilterRule("rates", "highlight")]
    assert evaluate(rules, "Fed holds rates", None) == (True, True)


def test_include_patterns_lowercases_and_filters_by_action():
    rules = [FilterRule("Fed", "include"), FilterRule("NVDA", "highlight")]
    assert include_patterns(rules) == ["fed"]


def test_highlights_ignores_include_rules():
    rules = [FilterRule("fed", "include")]
    assert highlights(rules, "Fed holds rates", None) is False


def test_highlights_matches_highlight_rules():
    rules = [FilterRule("nvidia", "highlight")]
    assert highlights(rules, "Nvidia beats", None) is True
    assert highlights(rules, "Oil slips", None) is False


def test_highlights_agrees_with_evaluate():
    rules = [FilterRule("fed", "include"), FilterRule("rates", "highlight")]
    assert highlights(rules, "Fed holds rates", None) == evaluate(
        rules, "Fed holds rates", None
    )[1]
