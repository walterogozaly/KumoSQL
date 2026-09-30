from kumosql import state


def test_state_round_trips_and_sections_are_independent():
    assert state.load_state() == {}
    state.set_section("ui", {"theme": "dark"})
    state.set_section("format", {"max_line_length": 100})
    assert state.get_section("ui") == {"theme": "dark"}
    assert state.get_section("format") == {"max_line_length": 100}
    assert state.get_section("missing", "fallback") == "fallback"


def test_corrupt_state_file_is_treated_as_empty():
    state.data_dir().mkdir(parents=True)
    state.state_path().write_text("{not json", encoding="utf-8")
    assert state.load_state() == {}
    state.set_section("ui", {"a": 1})
    assert state.get_section("ui") == {"a": 1}


def test_home_override_controls_location(tmp_path):
    assert state.data_dir() == tmp_path / "kumosql-home"


def test_concurrent_writers_do_not_lose_sections():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: state.set_section(f"s{i}", {"i": i}), range(40)))
    assert all(state.get_section(f"s{i}") == {"i": i} for i in range(40))
