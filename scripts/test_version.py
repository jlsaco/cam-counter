#!/usr/bin/env python3
"""Tests de scripts/version.py — cubre el camino SIN tags (estado real del repo hoy).

Crea un repo git temporal con 1 commit y SIN tag, ejecuta `version.py --json` con ese repo
como cwd y asegura: no lanza, `version` empieza por `0.0.0-dev.`, `git_sha` len >= 7, y las
cuatro claves presentes con tipos correctos. Sin dependencias externas (unittest stdlib).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_PY = os.path.join(HERE, "version.py")


def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _make_repo_without_tag(path):
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    with open(os.path.join(path, "dummy.txt"), "w", encoding="utf-8") as fh:
        fh.write("hola\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "primer commit sin tag"], path)


class TestVersionNoTag(unittest.TestCase):
    def test_json_no_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_without_tag(tmp)

            proc = subprocess.run(
                [sys.executable, VERSION_PY, "--json"],
                cwd=tmp,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # No debe fallar nunca por ausencia de tags.
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

            info = json.loads(proc.stdout)

            # Las cuatro claves exactas.
            self.assertEqual(
                set(info.keys()),
                {"version", "git_sha", "is_dirty", "is_release"},
            )
            # Degradación esperada sin tags.
            self.assertTrue(
                info["version"].startswith("0.0.0-dev."),
                msg=f"version inesperada: {info['version']}",
            )
            # git_sha de longitud >= 7.
            self.assertGreaterEqual(len(info["git_sha"]), 7)
            # Tipos.
            self.assertIsInstance(info["is_dirty"], bool)
            self.assertIsInstance(info["is_release"], bool)
            # Un commit limpio sin tag no es release.
            self.assertFalse(info["is_release"])

    def test_plain_no_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo_without_tag(tmp)

            proc = subprocess.run(
                [sys.executable, VERSION_PY],
                cwd=tmp,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            out = proc.stdout.strip()
            # String de versión no vacío y con la forma degradada.
            self.assertTrue(out)
            self.assertTrue(out.startswith("0.0.0-dev."), msg=out)


if __name__ == "__main__":
    unittest.main()
