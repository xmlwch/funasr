# -*- coding: utf-8 -*-
"""_expand_allowed_dirs glob 通配符展开测试

只测 main._expand_allowed_dirs 这个纯函数,不依赖 model / pool / handler。
"""
import os
import sys
import pytest


def _norm(p):
    """跨平台路径比较:Windows 上 / 和 \\ 等价,统一规范化。"""
    return os.path.normpath(p).replace('\\', '/')


class TestExpandLiteralPaths:
    """无通配符的路径应保持不变"""

    def test_literal_path_passes_through(self, tmp_path):
        from main import _expand_allowed_dirs
        path = str(tmp_path)
        result = _expand_allowed_dirs(path)
        assert [_norm(p) for p in result] == [_norm(path)]

    def test_home_expansion(self, tmp_path, monkeypatch):
        """`~` 应被展开(Windows 用 USERPROFILE,Unix 用 HOME)"""
        from main import _expand_allowed_dirs
        # Windows expanduser 优先看 USERPROFILE,Unix 看 HOME — 两个都设兼容
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setenv('USERPROFILE', str(tmp_path))
        result = _expand_allowed_dirs('~/uploads')
        assert [_norm(p) for p in result] == [_norm(str(tmp_path / 'uploads'))]

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        """`$VAR` 应被展开"""
        from main import _expand_allowed_dirs
        monkeypatch.setenv('FUNASR_TEST_DIR', str(tmp_path))
        result = _expand_allowed_dirs('$FUNASR_TEST_DIR/data')
        assert [_norm(p) for p in result] == [_norm(str(tmp_path / 'data'))]

    def test_comma_separated_literals(self, tmp_path):
        """多个字面路径逗号分隔"""
        from main import _expand_allowed_dirs
        p1 = str(tmp_path / 'a')
        p2 = str(tmp_path / 'b')
        os.makedirs(p1)
        os.makedirs(p2)
        result = _expand_allowed_dirs(f'{p1},{p2}')
        assert sorted(_norm(p) for p in result) == sorted([_norm(p1), _norm(p2)])


class TestExpandGlob:
    """glob 通配符展开"""

    def test_star_matches_direct_children(self, tmp_path):
        """`*` 匹配直接子项(非递归)"""
        from main import _expand_allowed_dirs
        # 创建 a/, b/, c/ 和 a/sub/
        for name in ('a', 'b', 'c'):
            (tmp_path / name).mkdir()
        (tmp_path / 'a' / 'sub').mkdir()

        pattern = str(tmp_path) + '/*'
        result = _expand_allowed_dirs(pattern)
        # 应只有 a, b, c(不包含 a/sub)
        names = sorted(os.path.basename(p) for p in result)
        assert names == ['a', 'b', 'c']

    def test_double_star_recursive(self, tmp_path):
        """`**` 匹配所有后代"""
        from main import _expand_allowed_dirs
        (tmp_path / 'a' / 'sub' / 'deep').mkdir(parents=True)
        (tmp_path / 'b').mkdir()

        pattern = str(tmp_path) + '/**'
        result = _expand_allowed_dirs(pattern)
        # ** 默认匹配所有文件和目录,包括 tmp_path 自身
        names = sorted(os.path.basename(p) for p in result if os.path.isdir(p) and p != str(tmp_path))
        assert 'a' in names and 'b' in names and 'sub' in names and 'deep' in names

    def test_question_mark_single_char(self, tmp_path):
        """`?` 匹配单个字符"""
        from main import _expand_allowed_dirs
        (tmp_path / 'a1').mkdir()
        (tmp_path / 'b1').mkdir()
        (tmp_path / 'cc').mkdir()  # 2 字符,不应匹配

        pattern = str(tmp_path) + '/?1'
        result = _expand_allowed_dirs(pattern)
        names = sorted(os.path.basename(p) for p in result)
        assert names == ['a1', 'b1']

    def test_brace_alternative(self, tmp_path):
        """`{a,b}` 风格不被原生 glob 支持,确认不影响行为"""
        from main import _expand_allowed_dirs
        for name in ('x', 'y'):
            (tmp_path / name).mkdir()
        # 原生 glob.glob 用 fnmatch,不展开 {} — 我们不主动支持,
        # 但模式字面量 {} 应被当作无通配直接当字面路径
        pattern = str(tmp_path) + '/{x,y}'
        result = _expand_allowed_dirs(pattern)
        # {} 在 glob 中视为字面 → 返回单个未匹配/或字面路径(取决于平台)
        # 我们不会主动支持,只确认不抛异常
        assert isinstance(result, list)

    def test_no_match_warns_but_no_error(self, tmp_path, caplog):
        """无匹配项应 logger.warning,不抛异常"""
        from main import _expand_allowed_dirs
        pattern = str(tmp_path) + '/nonexistent/*'
        result = _expand_allowed_dirs(pattern)
        assert result == []

    def test_max_results_exceeded_raises(self, tmp_path):
        """展开过多触发防 DoS"""
        from main import _expand_allowed_dirs
        # 创建 5 个子目录,设 max=3 应抛异常
        for i in range(5):
            (tmp_path / f'd{i}').mkdir()
        with pytest.raises(ValueError, match="展开过多"):
            _expand_allowed_dirs(str(tmp_path) + '/*', max_results=3)

    def test_comma_mixed_literal_and_glob(self, tmp_path):
        """混合字面和 glob"""
        from main import _expand_allowed_dirs
        literal_dir = tmp_path / 'literal'
        literal_dir.mkdir()
        glob_dir = tmp_path / 'glob'
        glob_dir.mkdir()

        pattern = f'{str(literal_dir)},{str(tmp_path)}/glob'
        result = _expand_allowed_dirs(pattern)
        assert any(p.endswith('literal') for p in result)
        assert any(p.endswith('glob') for p in result)


class TestRealpathCompatibility:
    """展开结果给后续 os.path.realpath 用"""

    def test_expanded_paths_realpath(self, tmp_path):
        """展开的 glob 结果经 realpath 仍可被 _is_safe_path 验证"""
        from main import _expand_allowed_dirs
        (tmp_path / 'sub').mkdir()
        target = tmp_path / 'sub' / 'file.png'
        target.write_text('x')

        # 模拟 _ALLOWED_DIRS 赋值
        allowed = [os.path.realpath(p) for p in
                   _expand_allowed_dirs(str(tmp_path) + '/*')]
        # 文件的 realpath 应在 allowed 内
        file_real = os.path.realpath(str(target))
        assert any(file_real == a or file_real.startswith(a + os.sep)
                   for a in allowed), \
            f"{file_real} 不在 {allowed}"
