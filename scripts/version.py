#!/usr/bin/env python3
"""Deriva el string de versión SemVer del proyecto desde tags git anotados.

Fuente de verdad de la versión = tags git SemVer (`vX.Y.Z`, prereleases `-rc.N`).
NO existe un archivo VERSION commiteado: la versión se deriva con
`git describe --tags`. Si NO hay tags (o el clon es shallow y no los ve), degrada
LIMPIO a `0.0.0-dev.<N>+g<sha>` sin lanzar NUNCA una excepción.

El MISMO string fluye por bundle-manifest, channel-manifest, device-registry y
`/api/device` (app_version).

Uso:
    python3 scripts/version.py            # imprime sólo el string de versión
    python3 scripts/version.py --version  # idem
    python3 scripts/version.py --json     # {version, git_sha, is_dirty, is_release}

Sin dependencias externas (sólo stdlib + git). Robusto a shallow clones y a la
ausencia total de git/commits.
"""
import argparse
import json
import re
import subprocess
import sys

# SemVer canónico (con prerelease/build opcionales). https://semver.org
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# describe --long: 'v1.2.3-4-gabc1234'  (tag, nº de commits extra, sha corto)
_DESCRIBE_RE = re.compile(r"^(?P<tag>.+)-(?P<n>\d+)-g(?P<sha>[0-9a-f]+)$")


def _git(args):
    """Ejecuta `git <args>` y devuelve stdout stripped, o None si falla.

    Nunca lanza: captura git ausente, repo ausente y exit codes no-cero.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _strip_v(tag):
    """'v1.2.3' -> '1.2.3'; '1.2.3-rc.1' -> '1.2.3-rc.1'."""
    return tag[1:] if tag.startswith("v") else tag


def _is_semver(value):
    return bool(_SEMVER_RE.match(value))


def derive():
    """Devuelve (version, git_sha, is_dirty, is_release). Nunca lanza."""
    # git_sha: siempre longitud >= 7 (degrada a "unknown", que tiene 7 chars).
    sha = _git(["rev-parse", "--short=7", "HEAD"]) or "unknown"

    # nº de commits (para la forma dev sin tags); fallback a 0.
    count_raw = _git(["rev-list", "--count", "HEAD"])
    try:
        count = int(count_raw) if count_raw is not None else 0
    except ValueError:
        count = 0

    # árbol sucio: porcelain no vacío. Si git falla, no-sucio.
    porcelain = _git(["status", "--porcelain"])
    is_dirty = bool(porcelain)

    # Tag SemVer más cercano (formato largo uniforme). Filtra a tags 'vX...'.
    described = _git(["describe", "--tags", "--long", "--match", "v[0-9]*"])
    if described:
        m = _DESCRIBE_RE.match(described)
        if m:
            base = _strip_v(m.group("tag"))
            n = int(m.group("n"))
            if n == 0 and not is_dirty and _is_semver(base):
                # HEAD exactamente en un tag SemVer limpio -> release.
                return base, sha, is_dirty, True
            # Hay tag pero con commits extra y/o dirty -> prerelease dev.
            return f"{base}-dev.{n}+g{sha}", sha, is_dirty, False

    # Sin tags (o describe no encontró match): degrada limpio.
    return f"0.0.0-dev.{count}+g{sha}", sha, is_dirty, False


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deriva la versión SemVer del proyecto desde tags git "
        "(degrada limpio a 0.0.0-dev.N+g<sha> sin tags).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--json",
        action="store_true",
        help="Imprime {version, git_sha, is_dirty, is_release} como JSON.",
    )
    group.add_argument(
        "--version",
        action="store_true",
        help="Imprime sólo el string de versión (comportamiento por defecto).",
    )
    args = parser.parse_args(argv)

    version, git_sha, is_dirty, is_release = derive()

    if args.json:
        print(
            json.dumps(
                {
                    "version": version,
                    "git_sha": git_sha,
                    "is_dirty": is_dirty,
                    "is_release": is_release,
                }
            )
        )
    else:
        print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
