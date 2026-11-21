"""Deterministic bundle tests: no model traffic, file writes or code execution."""
import io
import json
from unittest import TestCase
from unittest.mock import patch
import zipfile

from .bundles import BundleError, check_bundle, parse_bundle, validate_bundle, zip_bundle


class BundleTests(TestCase):
    def bundle(self, *files):
        return {'summary': '可继续修改的示例工程',
                'files': [{'path': path, 'content': content} for path, content in files]}

    def test_nested_project_roundtrips_as_safe_deterministic_utf8_zip(self):
        bundle = self.bundle(('README.md', '# 学习计划\n'), ('src/main.py', 'print("hello")\n'),
                             ('web/index.html', '<!doctype html><html><body><p>你好</body></html>'),
                             ('web/style.css', 'body { color: black; }'), ('web/app.js', 'console.log("hello");'))
        parsed = parse_bundle(json.dumps(bundle, ensure_ascii=False))
        self.assertEqual(parsed, bundle)
        self.assertIsNot(parsed, bundle)
        self.assertTrue(check_bundle(parsed)['passed'])
        raw = zip_bundle(parsed)
        self.assertEqual(raw, zip_bundle(parsed))
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(set(archive.namelist()), {item['path'] for item in bundle['files']})
            for item in bundle['files']:
                self.assertEqual(archive.read(item['path']).decode('utf-8'), item['content'])
                self.assertEqual(archive.getinfo(item['path']).date_time, (1980, 1, 1, 0, 0, 0))

    def test_rejects_path_traversal_windows_names_and_ambiguous_paths(self):
        paths = ['../a.py', './a.py', '/a.py', 'C:/a.py', '//host/share/a.py', 'a\\b.py',
                 'src//a.py', 'src/../a.py', 'src/./a.py', 'src /a.py', 'a.py ', 'src./a.py',
                 'src/con.txt', 'AUX.json', 'com1.py', 'LPT9.md', 'COM¹.txt', 'CONIN$.txt', 'CON .txt',
                 'a\x00.py', 'a\n.py', 'a\u202e.py', 'src:.py', 'a?.py', 'a*.py',
                 '.env', '.env.txt', '.git/config.txt', 'src/．/a.py', 'a：b.py', 'a／b.py',
                 'output.exe', 'launch.cmd', 'archive.zip', 'credentials.key', 'README']
        for path in paths:
            with self.subTest(path=path), self.assertRaises(BundleError):
                validate_bundle(self.bundle((path, 'sample')))

    def test_rejects_casefold_unicode_duplicates_and_file_directory_conflicts(self):
        pairs = [('a.py', 'a.py'), ('Readme.md', 'README.MD'), ('a.py', 'ａ.py'),
                 ('file.py', 'file.py/other.py'), ('File.py/sub.py', 'file.PY')]
        for first, second in pairs:
            with self.subTest(paths=(first, second)), self.assertRaises(BundleError):
                validate_bundle(self.bundle((first, ''), (second, '')))

    def test_strict_json_schema_duplicate_fields_and_limits(self):
        invalid = ['{"summary":"x","summary":"y","files":[]}',
                   '{"summary":"x","files":[{"path":"a.py","path":"b.py","content":""}]}',
                   '```json\n{}\n```', '{"summary":NaN,"files":[]}', '[]']
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(BundleError):
                parse_bundle(raw)
        invalid_objects = [self.bundle(), {**self.bundle(('x.md', '')), 'extra': True},
                           {'summary': 'x', 'files': [{'path': 'x.txt', 'content': '', 'owner': 'other'}]},
                           {'summary': 'x' * 2001, 'files': [{'path': 'x.txt', 'content': ''}]},
                           self.bundle(*[(f'{index}.txt', '') for index in range(13)]),
                           self.bundle(('a.txt', 'x' * 30001)),
                           self.bundle(*[(f'{index}.txt', 'x' * 26000) for index in range(4)]),
                           self.bundle(('a.txt', '\ud800'))]
        for bundle in invalid_objects:
            with self.subTest(description=str(bundle)[:50]), self.assertRaises(BundleError):
                validate_bundle(bundle)

    def test_direct_check_and_zip_cannot_bypass_validation(self):
        malicious = self.bundle(('../outside.py', 'print(1)'))
        for operation in (check_bundle, zip_bundle):
            with self.subTest(operation=operation.__name__), self.assertRaises(BundleError):
                operation(malicious)

    def test_python_is_parsed_but_never_executed(self):
        code = 'raise RuntimeError("do not run")\nopen("never-touch.txt", "w").write("unsafe")\n'
        with patch('builtins.open', side_effect=AssertionError('generated code must not run')) as opened:
            report = check_bundle(self.bundle(('danger.py', code)))
        opened.assert_not_called()
        self.assertTrue(report['passed'])
        self.assertTrue(any(item['check'] == 'Python 运行与测试' and item['status'] == 'not_run'
                            for item in report['checks']))

    def test_python_syntax_and_statement_context_failures_are_recorded(self):
        for code in ('def missing(:\n    pass', 'return 42', '\x00'):
            with self.subTest(code=code):
                report = check_bundle(self.bundle(('bad.py', code)))
                self.assertFalse(report['passed'])
                self.assertTrue(any(item['check'] == 'Python 静态语法' and item['status'] == 'failed'
                                    for item in report['checks']))

    def test_json_rejects_duplicate_keys_nonfinite_and_malformed_content(self):
        for content in ('{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}', '{"x":NaN}', '{', ''):
            with self.subTest(content=content):
                self.assertFalse(check_bundle(self.bundle(('bad.json', content)))['passed'])
        self.assertTrue(check_bundle(self.bundle(('data.json', '{"items":[1,true,null]}')))['passed'])

    def test_csv_checks_shape_and_quotes_without_evaluating_formulas(self):
        for content in ('', '\n\n', 'a,b\n1\n', 'a,b\n"unterminated,2'):
            with self.subTest(content=content):
                self.assertFalse(check_bundle(self.bundle(('bad.csv', content)))['passed'])
        report = check_bundle(self.bundle(('data.csv', 'name,value\n"two, words",=1+1\n')))
        self.assertTrue(report['passed'])
        self.assertIn('未计算公式', report['checks'][-1]['detail'])

    def test_html_accepts_optional_end_tags_but_discloses_no_browser_validation(self):
        report = check_bundle(self.bundle(('index.html', '<ul><li>甲<li>乙</ul>')))
        self.assertTrue(report['passed'])
        self.assertTrue(any(item['status'] == 'not_run' for item in report['checks']))
        self.assertFalse(check_bundle(self.bundle(('bad.html', '<div')))['passed'])
        self.assertFalse(check_bundle(self.bundle(('bad.html', 'just plain text')))['passed'])

    def test_js_css_unchecked_syntax_does_not_become_a_false_pass_claim(self):
        report = check_bundle(self.bundle(('app.js', 'syntactically invalid {{{'), ('app.css', 'invalid {{{')))
        self.assertTrue(report['passed'])
        self.assertEqual(sum(item['status'] == 'not_run' for item in report['checks']), 2)
        self.assertTrue(all(item['check'] == '路径与文本格式' for item in report['checks']
                            if item['status'] == 'passed'))
        self.assertIn('不执行生成的代码', report['scope'])
