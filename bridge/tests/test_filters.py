from app import filters

F = filters.DEFAULTS


def hidden(sender, text="hello"):
    return filters.reason_with(F, {"sender": sender, "text": text}) is not None


def test_default_rules_catch_bot_names():
    assert hidden("QX7KD2PL")
    assert hidden(")NT/LQN5")
    assert hidden("G0OD£=G9")


def test_default_rules_leave_people_alone():
    assert not hidden("Rowan ☀️")
    assert not hidden("G7VQV/P1")          # callsign with /P suffix
    assert not hidden("basecamp-west")


def test_bad_regex_is_ignored():
    f = {**F, "sender_regex": ["(unclosed"], "text_regex": []}
    assert filters.reason_with(f, {"sender": "anything", "text": "x"}) is None
