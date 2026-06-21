#!/usr/bin/env python3
"""Tests de scripts/version.py — cubre el camino SIN tags (degradación limpia).

Compatible con `python3 -m pytest scripts/test_version.py` y con la ejecución
directa `python3 scripts/test_version.py` (vía unittest.main()).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_PY = os.path.join(HERE, "version.py")


def _run_git(cwd, *args):
    env = dict(os.environ)
    # Identidad fija para que el commit funcione sin config global (CI/x86).
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


class TestVersionNoTag(unittest.TestCase):
    def test_no_tag_degrades_cleanly(self):
        with tempfile.TemporaryDirectory() as repo:
            # Repo git temporal con 1 commit y SIN tags.
            _run_git(repo, "init", "-q")
            with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as fh:
                fh.write("hello\n")
            _run_git(repo, "add", "f.txt")
            _run_git(repo, "commit", "-q", "-m", "initial")

            # Ejecuta version.py --json con el repo temporal como cwd.
            proc = subprocess.run(
                [sys.executable, VERSION_PY, "--json"],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            # 1) No excepción / exit limpio.
            self.assertEqual(
                proc.returncode, 0, msg=f"stderr={proc.stderr!r}"
            )

            # 2) JSON válido con EXACTAMENTE las cuatro claves.
            data = json.loads(proc.stdout)
            self.assertEqual(
                set(data.keys()),
                {"version", "git_sha", "is_dirty", "is_release"},
            )

            # 3) Sin tags -> version degradada a 0.0.0-dev.<N>+g<sha>.
            self.assertTrue(
                data["version"].startswith("0.0.0-dev."),
                msg=f"version inesperada: {data['version']!r}",
            )

            # 4) git_sha de longitud >= 7.
            self.assertGreaterEqual(len(data["git_sha"]), 7)

            # 5) Tipos correctos; sin tag no es release; árbol limpio.
            self.assertIsInstance(data["is_dirty"], bool)
            self.assertIsInstance(data["is_release"], bool)
            self.assertFalse(data["is_release"])
            self.assertFalse(data["is_dirty"])

    def test_plain_output_prints_version_string(self):
        with tempfile.TemporaryDirectory() as repo:
            _run_git(repo, "init", "-q")
            with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as fh:
                fh.write("hi\n")
            _run_git(repo, "add", "f.txt")
            _run_git(repo, "commit", "-q", "-m", "initial")

            proc = subprocess.run(
                [sys.executable, VERSION_PY],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr!r}")
            out = proc.stdout.strip()
            self.assertTrue(out, msg="el string de versión no debe estar vacío")
            self.assertTrue(out.startswith("0.0.0-dev."))


if __name__ == "__main__":
    unittest.main()
