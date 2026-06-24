"""Validación fail-closed al boot + sonda HTTP de liveness para el edge dockerizado.

Este módulo concentra DOS responsabilidades del WP17 (dockerización), ambas
pensadas para que un contenedor mal configurado **falle rápido y ruidoso** en vez
de arrancar mudo y reintentar para siempre:

1. ``boot_problems(env)`` — validación PURA que el ENTRYPOINT del contenedor (y la
   unit systemd como plan B) corre UNA VEZ al boot, ANTES de arrancar el
   supervisor. Devuelve la lista de problemas (vacía = OK):

   - ``site_id`` / ``device_id`` son slugs válidos (regex de ``identifiers``);
   - en transporte ``iot`` (fail-closed del corte WP16): el ``thing_name`` casa el
     canon ``cam-counter-{site_id}-{device_id}`` (la policy IoT ata client-id ==
     Thing), hay ``endpoint`` y ``region``, y el material mTLS (cert/key/CA) existe
     y es legible, con la **llave privada en 0600** (ni grupo ni otros).

   Fail-closed: cualquier problema => abortar el boot. En transporte ``direct`` (o
   con el sync apagado) NO se exige material IoT (degradación limpia).

2. CLI para el contenedor:

   - ``python -m cam_counter_edge.healthcheck boot`` corre (1) y sale ``!=0`` si hay
     problemas (lo usa el ENTRYPOINT del servicio ``edge``).
   - ``python -m cam_counter_edge.healthcheck http [url]`` hace un ``GET`` barato a
     ``/healthz`` (por defecto ``http://127.0.0.1:${CAMCOUNTER_HEALTHZ_PORT}``) y
     sale ``0``/``1`` — lo usa el ``HEALTHCHECK`` de Docker / compose.

No importa boto3/paho ni abre SQLite: sólo lee entorno y hace ``stat`` de ficheros,
de modo que es seguro importarlo (y testearlo) sin dependencias de hardware/nube.
"""

from __future__ import annotations

import os
import stat as _stat
import sys
import urllib.request
from collections.abc import Callable, Mapping

from .identifiers import is_valid_slug

__all__ = [
    "PRODUCT_PREFIX",
    "boot_problems",
    "canonical_thing_name",
    "http_probe",
    "main",
]

# Prefijo de producto del Thing IoT (idéntico a mqtt_publisher.PRODUCT_PREFIX y a la
# convención `cam-counter-<recurso>` del resto del monorepo). NO es un secreto.
PRODUCT_PREFIX = "cam-counter"

# Transportes válidos del sync (espejo de sync_dispatch.VALID_TRANSPORTS; se redefine
# aquí para no importar ese módulo —y arrastrar sus deps— sólo por dos constantes).
_TRANSPORT_DIRECT = "direct"
_TRANSPORT_IOT = "iot"
_ENABLED_TRUE = {"1", "true", "yes", "on"}

# Material mTLS OBLIGATORIO del device en modo ``iot`` (lo escribe provision-device.sh).
_IOT_CERT_VARS = (
    "CAMCOUNTER_IOT_CERT_PATH",
    "CAMCOUNTER_IOT_KEY_PATH",
    "CAMCOUNTER_IOT_ROOT_CA_PATH",
)
# Cuál de las anteriores es la LLAVE PRIVADA (exige 0600: ni grupo ni otros).
_IOT_KEY_VAR = "CAMCOUNTER_IOT_KEY_PATH"


def canonical_thing_name(site_id: str, device_id: str) -> str:
    """Nombre canónico del Thing IoT del device: ``cam-counter-{site}-{device}``.

    Es el client-id == Thing que ata la policy IoT (WP06); el ``thing_name`` del
    entorno DEBE coincidir exactamente con éste.
    """
    return f"{PRODUCT_PREFIX}-{site_id}-{device_id}"


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _flag(env: Mapping[str, str], name: str) -> bool:
    return _env(env, name).lower() in _ENABLED_TRUE


def _resolve_transport(env: Mapping[str, str]) -> str:
    """``CAMCOUNTER_SYNC_TRANSPORT`` normalizado (default ``direct``, como el dispatch)."""
    return _env(env, "CAMCOUNTER_SYNC_TRANSPORT").lower() or _TRANSPORT_DIRECT


def _iot_problems(
    env: Mapping[str, str],
    *,
    isfile: Callable[[str], bool],
    mode_of: Callable[[str], int | None],
) -> list[str]:
    """Problemas del material/identidad IoT (sólo se llama en transporte ``iot``)."""
    problems: list[str] = []
    site_id = _env(env, "CAMCOUNTER_SITE_ID")
    device_id = _env(env, "CAMCOUNTER_DEVICE_ID")

    # Thing name == canon. Acepta CAMCOUNTER_IOT_THING_NAME o el alias _CLIENT_ID.
    thing_name = _env(env, "CAMCOUNTER_IOT_THING_NAME") or _env(
        env, "CAMCOUNTER_IOT_CLIENT_ID"
    )
    if not thing_name:
        problems.append("falta CAMCOUNTER_IOT_THING_NAME (identidad IoT del device)")
    elif is_valid_slug(site_id) and is_valid_slug(device_id):
        expected = canonical_thing_name(site_id, device_id)
        if thing_name != expected:
            problems.append(
                f"CAMCOUNTER_IOT_THING_NAME={thing_name!r} != canon {expected!r} "
                f"(cam-counter-<site>-<device>); el topic/policy divergiría"
            )

    if not _env(env, "CAMCOUNTER_IOT_ENDPOINT"):
        problems.append("falta CAMCOUNTER_IOT_ENDPOINT (endpoint ATS de IoT Core)")
    if not _env(env, "CAMCOUNTER_AWS_REGION"):
        problems.append("falta CAMCOUNTER_AWS_REGION (región AWS)")

    # Material mTLS: cada fichero definido y legible.
    for name in _IOT_CERT_VARS:
        path = _env(env, name)
        if not path:
            problems.append(f"falta {name} (material mTLS del device)")
        elif not isfile(path):
            problems.append(f"{name}={path!r} no existe o no es legible")

    # Llave privada: permisos 0600 (ni grupo ni otros). Defensa en profundidad: una
    # llave 0644 montada por error es un secreto expuesto a otros usuarios del host.
    key_path = _env(env, _IOT_KEY_VAR)
    if key_path and isfile(key_path):
        mode = mode_of(key_path)
        if mode is not None and (mode & 0o077):
            problems.append(
                f"{_IOT_KEY_VAR}={key_path!r} tiene permisos {oct(mode & 0o777)} "
                f"(se exige 0600: la llave privada no debe ser legible por grupo/otros)"
            )
    return problems


def _default_mode_of(path: str) -> int | None:
    try:
        return _stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def boot_problems(
    env: Mapping[str, str] | None = None,
    *,
    isfile: Callable[[str], bool] | None = None,
    mode_of: Callable[[str], int | None] | None = None,
) -> list[str]:
    """Lista de problemas de configuración al boot (vacía = arranque permitido).

    Pura y fail-closed: valida slugs siempre y, en transporte ``iot`` con el sync
    activo, exige identidad+material IoT consistentes (ver módulo). ``isfile`` /
    ``mode_of`` son inyectables para tests (por defecto ``os.path.isfile`` y el
    modo POSIX real del fichero).
    """
    env = os.environ if env is None else env
    isfile = os.path.isfile if isfile is None else isfile
    mode_of = _default_mode_of if mode_of is None else mode_of

    problems: list[str] = []

    site_id = _env(env, "CAMCOUNTER_SITE_ID")
    device_id = _env(env, "CAMCOUNTER_DEVICE_ID")
    if not is_valid_slug(site_id):
        problems.append(
            f"CAMCOUNTER_SITE_ID={site_id!r} no es un slug válido "
            f"(^[a-z0-9][a-z0-9-]{{1,62}}$)"
        )
    if not is_valid_slug(device_id):
        problems.append(
            f"CAMCOUNTER_DEVICE_ID={device_id!r} no es un slug válido "
            f"(^[a-z0-9][a-z0-9-]{{1,62}}$)"
        )

    # El material IoT sólo es obligatorio en transporte iot CON el sync activo: en
    # direct (o sync apagado) el device no usa certificado (degradación limpia).
    if _flag(env, "CAMCOUNTER_SYNC_ENABLED") and _resolve_transport(env) == _TRANSPORT_IOT:
        problems.extend(_iot_problems(env, isfile=isfile, mode_of=mode_of))

    return problems


def http_probe(url: str, *, timeout: float = 3.0) -> bool:
    """``GET url`` y devuelve ``True`` si responde 2xx. No lanza (sonda de liveness)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — URL local
            return 200 <= getattr(resp, "status", resp.getcode()) < 300
    except Exception:  # noqa: BLE001 — cualquier fallo = no-sano
        return False


def _default_healthz_url(env: Mapping[str, str]) -> str:
    port = _env(env, "CAMCOUNTER_HEALTHZ_PORT") or "8081"
    return f"http://127.0.0.1:{port}/healthz"


def main(argv: list[str] | None = None) -> int:
    """CLI: ``boot`` (validación fail-closed) | ``http [url]`` (sonda de liveness)."""
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "boot"

    if cmd == "boot":
        problems = boot_problems(os.environ)
        if problems:
            print("cam-counter boot fail-closed: configuración inválida:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("cam-counter boot: configuración válida (OK).")
        return 0

    if cmd == "http":
        url = args[1] if len(args) > 1 else _default_healthz_url(os.environ)
        ok = http_probe(url)
        if not ok:
            print(f"cam-counter healthcheck: {url} no responde sano", file=sys.stderr)
        return 0 if ok else 1

    print(f"uso: {sys.argv[0]} [boot|http [url]]", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
