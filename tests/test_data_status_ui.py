import ast
from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_data_status_cards_preserve_full_text_and_wrap():
    source = Path('app.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    helper = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == 'render_data_source_status'), None)
    assert helper is not None, 'Five metrics in a narrow nested column truncate status text'
    app = AppTest.from_string('import streamlit as st\n' + ast.get_source_segment(source, helper)
        + '\nrender_data_source_status(True, 4, False, "连接验证成功", "2026-10-22")').run()
    assert not app.exception
    markup = '\n'.join(m.value for m in app.markdown)
    for text in ['AKShare', '本地 iFinD CSV', 'iFinD 程序', 'iFinD 数据连接',
                 '数据库最新日期', '可用', '4 个', '未安装', '连接验证成功', '2026-10-22']:
        assert text in markup
    assert 'auto-fit' in markup
    assert 'white-space: normal' in markup
    assert 'overflow-wrap: anywhere' in markup
    assert 'render_data_source_status(' in source[source.index('def render_data_source_status') + len(ast.get_source_segment(source, helper)):]
