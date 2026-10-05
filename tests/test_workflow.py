"""Обёртка unittest над tests/check_workflow.py, чтобы его проверки шли вместе со всеми тестами
(python -m unittest discover -s tests -p "test_*.py"): .github/workflows/update.yml, закрытые файлы и заказы
не публикуются и не архивируются, пробная публикация deploy.py без сети (git push/fetch подменены).

    python -m unittest tests.test_workflow
    python tests/check_workflow.py          # то же самое с подробной печатью
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import check_workflow  # noqa: E402


class WorkflowTest(unittest.TestCase):
    def test_workflow_file_and_private_storage(self):
        """Структура update.yml, порядок шагов, FORBIDDEN в deploy.py, .gitignore и datastore."""
        errs = check_workflow.check()
        self.assertEqual(errs, [], "\n".join(errs))

    def test_workflow_parses(self):
        wf = check_workflow.parse_yaml(check_workflow.WF.read_text(encoding="utf-8"))
        self.assertIn("jobs", wf)
        job = next(iter(wf["jobs"].values()))
        self.assertTrue(job.get("steps"))
        self.assertTrue(all(isinstance(s, dict) for s in job["steps"]))

    def test_yaml_mini_parser(self):
        text = ("name: t  # комментарий\non:\n  schedule:\n    - cron: '0 */6 * * *'\njobs:\n  a:\n    steps:\n"
                "      - name: x\n        run: |\n          python run.py\n          echo ok\n      - uses: a/b@v4\n")
        wf = check_workflow.parse_yaml(text)
        self.assertEqual(wf["name"], "t")
        self.assertEqual(wf["on"]["schedule"], [{"cron": "0 */6 * * *"}])
        steps = wf["jobs"]["a"]["steps"]
        self.assertEqual(steps[0]["run"], "python run.py\necho ok\n")
        self.assertEqual(steps[1], {"uses": "a/b@v4"})
        with self.assertRaises(check_workflow.YamlError):
            check_workflow.parse_yaml("a:\n\tb: 1\n")                           # табуляция
        with self.assertRaises(check_workflow.YamlError):
            check_workflow.parse_yaml("a: 1\na: 2\n")                           # повтор ключа

    def test_deploy_dry_run_without_network(self):
        """Пробная публикация deploy.py во временную папку: теги превью, og.png, нет закрытых файлов,
        предупреждение о контакте, gh-pages — один коммит без родителей."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            errs = check_workflow.check_deploy_run()
        self.assertEqual(errs, [], "\n".join(errs) + "\n" + out.getvalue()[-2000:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
