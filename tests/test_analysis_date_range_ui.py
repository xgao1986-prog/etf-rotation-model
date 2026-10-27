"""Exercise the actual date selector without running a strategy backtest."""

import ast
from pathlib import Path

from streamlit.testing.v1 import AppTest


def selector_source():
    source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef)
         and node.name == "render_analysis_date_range"),
        None,
    )
    assert helper is not None, "The analysis selector must display trading dates, not row numbers"
    return ast.get_source_segment(source, helper)


def make_app(dates):
    source = (
        "import streamlit as st\nfrom datetime import date\n"
        + selector_source()
        + f"\ndates = [date.fromisoformat(d) for d in {dates!r}]\n"
        + "start, end = render_analysis_date_range(dates)\n"
        + "st.text(f'SELECTION: {start} / {end}')\n"
    )
    return AppTest.from_string(source).run()


def test_dates_replace_numbers_and_selection_stays_on_trading_days():
    dates = ["2024-04-18", "2024-04-19", "2024-04-22", "2024-04-23"]
    app = make_app(dates)
    assert not app.exception
    slider = app.select_slider[0]
    assert slider.options == dates
    assert app.text[0].value == "SELECTION: 2024-04-18 / 2024-04-23"
    slider.set_range("2024-04-19", "2024-04-22").run()
    assert not app.exception
    assert app.text[0].value == "SELECTION: 2024-04-19 / 2024-04-22"
    assert "2 个交易日" in app.caption[0].value


def test_axis_includes_first_and_last_dates():
    app = make_app(["2019-06-03", "2020-01-02", "2023-01-03", "2026-10-21"])
    assert not app.exception
    axis = "\n".join(item.value for item in app.markdown)
    assert "2019-06-03" in axis
    assert "2026-10-21" in axis


def test_empty_and_one_day_ranges():
    for dates, expected in [([], "None / None"), (["2026-10-21"], "2026-10-21 / 2026-10-21")]:
        app = make_app(dates)
        assert not app.exception
        assert not app.select_slider
        assert app.text[0].value == f"SELECTION: {expected}"


def test_handles_can_select_the_same_trading_day():
    app = make_app(["2024-04-19", "2024-04-22", "2024-04-23"])
    app.select_slider[0].set_range("2024-04-22", "2024-04-22").run()
    assert not app.exception
    assert app.text[0].value == "SELECTION: 2024-04-22 / 2024-04-22"
    assert "1 个交易日" in app.caption[0].value
