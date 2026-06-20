#!/usr/bin/env python3
"""Deriva la versión SemVer canónica del repo a partir de tags git anotados.

Fuente de verdad: tags git anotados `vX.Y.Z` (prereleases `-rc.N`) vía
`git describe --tags --always --dirty`. NO hay archivo VERSION commiteado.

La misma cadena fluye por bundle-manifest, channel-manifest, device-registry y
`/api/device`. Hoy el repo NO tiene tags; el script debe **degradar limpio** (nunca lanzar
excepción) a `0.0.0-dev.<N>+g<sha>`, y ser robusto a shallow clones y a la ausencia total
de git.

Uso:
  python3 scripts/version.py            # imprime solo el string de versión
  python3 scripts/version.py --version  # idem
  python3 scripts/version.py --json     # {version, git_sha, is_dirty, is_release}

Sin dependencias externas: solo stdlib + git.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# Default seguro si no hay git en absoluto (sha de exactamente 7 chars).
UNKNOWN_SHA = "unknown"  # 7 caracteres

# Tag SemVer limpio: v1.2.3 o v1.2.3-rc.4 (con o sin prefijo 'v').
_SEMVER_TAG_RE = re.compile(
    r"^v?(?P<core>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$"
)


def _run_git(args: list[str]) -> "str | None":
    """Ejecuta `git <args>` y devuelve stdout stripped, o None si falla.

    Nunca lanza: cualquier error (git ausente, no es repo, sin tags, shallow) -> None.
    """
    try:
        out = subprocess.run(
            ["git", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _short_sha() -> str:
    """SHA corto de 7 chars de HEAD; UNKNOWN_SHA (7 chars) si no hay git/commits."""
    sha = _run_git(["rev-parse", "--short=7", "HEAD"])
    if sha:
        return sha
    return UNKNOWN_SHA


def _commit_count() -> int:
    """Nº de commits alcanzables desde HEAD; 0 si no hay git/commits."""
    n = _run_git(["rev-list", "--count", "HEAD"])
    if n and n.isdigit():
        return int(n)
    return 0


def _is_dirty() -> bool:
    """True si el árbol de trabajo tiene cambios sin commitear."""
    status = _run_git(["status", "--porcelain"])
    # status es "" (limpio) o None (sin git) -> no dirty; con contenido -> dirty.
    return bool(status)


def _exact_semver_tag() -> "str | None":
    """Si HEAD está EXACTAMENTE en un tag SemVer (sin commits extra), devuelve su 'core'.

    Devuelve la parte SemVer sin prefijo 'v' (p.ej. '1.2.3' o '1.2.3-rc.4'), o None.
    """
    tag = _run_git(["describe", "--tags", "--exact-match", "HEAD"])
    if not tag:
        return None
    m = _SEMVER_TAG_RE.match(tag)
    if not m:
        return None
    return m.group("core")


def compute_version() -> dict:
    """Calcula version/git_sha/is_dirty/is_release sin lanzar nunca."""
    git_sha = _short_sha()
    is_dirty = _is_dirty()

    exact = _exact_semver_tag()
    if exact is not None:
        # HEAD justo en un tag SemVer limpio. Es release sólo si además no está dirty.
        version = exact
        is_release = not is_dirty
        if is_dirty:
            # Tag exacto pero árbol sucio: marca la suciedad sin pretender ser release.
            version = f"{exact}+dirty.g{git_sha}"
        return {
            "version": version,
            "git_sha": git_sha,
            "is_dirty": is_dirty,
            "is_release": is_release,
        }

    # No estamos en un tag exacto: intentar describe contra el último tag SemVer.
    described = _run_git(["describe", "--tags", "--long", "--always", "--dirty"])
    if described:
        # Formato esperado con tags: <tag>-<N>-g<sha>[-dirty]; con --always y sin tags: <sha>[-dirty].
        m = re.match(
            r"^v?(?P<core>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)-(?P<n>\d+)-g(?P<sha>[0-9a-f]+)(?P<dirty>-dirty)?$",
            described,
        )
        if m:
            core = m.group("core")
            n = m.group("n")
            version = f"{core}-dev.{n}+g{git_sha}"
            return {
                "version": version,
                "git_sha": git_sha,
                "is_dirty": is_dirty,
                "is_release": False,
            }

    # Sin tags (o describe sólo devolvió un sha): degradar a 0.0.0-dev.<N>+g<sha>.
    n = _commit_count()
    version = f"0.0.0-dev.{n}+g{git_sha}"
    return {
        "version": version,
        "git_sha": git_sha,
        "is_dirty": is_dirty,
        "is_release": False,
    }


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deriva la versión SemVer del repo (degrada limpio sin tags).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime un objeto JSON {version, git_sha, is_dirty, is_release}.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Imprime solo el string de versión (comportamiento por defecto).",
    )
    args = parser.parse_args(argv)

    info = compute_version()

    if args.json:
        print(json.dumps(info))
    else:
        print(info["version"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
