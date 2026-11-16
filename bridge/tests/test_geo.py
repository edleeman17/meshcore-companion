from app.geo import clean, ok_pos


def test_real_positions_pass():
    assert ok_pos(51.50, -0.12)
    assert ok_pos(-33.9, 151.2)


def test_impossible_positions_fail():
    assert not ok_pos(85.74, 2081.05)     # seen on air: lon 2081
    assert not ok_pos(0, 0)               # no GPS fix
    assert not ok_pos(None, 1)
    assert not ok_pos("x", 1)


def test_clean_zeroes_bad_and_keeps_good():
    assert clean({"lat": 85.7, "lon": 2081.0}) == {"lat": 0.0, "lon": 0.0}
    assert clean({"lat": 50.8, "lon": -1.1}) == {"lat": 50.8, "lon": -1.1}
    assert clean({"lat": None, "lon": None}) == {"lat": None, "lon": None}   # unlocated stays as is
