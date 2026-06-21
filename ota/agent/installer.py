"""Instalación atómica + activación + rollback (layout immutable-releases/current/shared).

Invariantes DUROS:
  - `releases/<version>/` inmutable, una carpeta por versión instalada.
  - `current` = symlink atómico (`os.replace`) a la release activa. `releases/` y `current`
    viven en el MISMO filesystem (`install_root`) -> rename atómico garantizado.
  - `shared/` = estado mutable que sobrevive a cada upgrade/rollback; puede ser OTRO mount
    (sólo datos): NUNCA se cruza con un rename atómico. Aquí viven `last_good` y los
    failed-markers (sobreviven a reboot).
  - NUNCA se borra `last_good`; se conservan >= `min_releases_retained` releases.
  - Los ficheros `.part` se descartan al ARRANQUE del agente (no sólo al boot del SO).
"""
import os
import shutil
import tarfile
import tempfile


class InstallError(Exception):
    """Fallo durante extracción / activación / rollback."""


class Installer:
    def __init__(self, cfg):
        self.cfg = cfg

    # ── rutas ───────────────────────────────────────────────────────────────
    def release_path(self, version):
        return os.path.join(self.cfg.releases_dir, version)

    def _failed_marker(self, version):
        return os.path.join(self.cfg.state_dir, f"failed-{version}")

    def _last_good_file(self):
        return os.path.join(self.cfg.state_dir, "last_good")

    def ensure_dirs(self):
        os.makedirs(self.cfg.releases_dir, exist_ok=True)
        os.makedirs(self.cfg.state_dir, exist_ok=True)

    # ── versión activa ───────────────────────────────────────────────────────
    def current_version(self):
        """basename del destino del symlink `current`, o None si no existe."""
        link = self.cfg.current_link
        if not os.path.islink(link):
            return None
        target = os.readlink(link)
        return os.path.basename(os.path.normpath(target))

    # ── last_good (persistente en shared/) ────────────────────────────────────
    def last_good(self):
        path = self._last_good_file()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                v = fh.read().strip()
            return v or None
        return None

    def set_last_good(self, version):
        self.ensure_dirs()
        tmp = self._last_good_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(version)
        os.replace(tmp, self._last_good_file())  # mismo FS (shared/)

    # ── failed-markers por versión (no se reintenta) ──────────────────────────
    def is_failed(self, version):
        return os.path.exists(self._failed_marker(version))

    def mark_failed(self, version, error=""):
        self.ensure_dirs()
        with open(self._failed_marker(version), "w", encoding="utf-8") as fh:
            fh.write(error or "")

    def clear_failed(self, version):
        try:
            os.remove(self._failed_marker(version))
        except FileNotFoundError:
            pass

    # ── limpieza de .part al arranque del agente ──────────────────────────────
    def discard_part_files(self):
        """Borra restos `*.part` (descargas/extracciones a medias) bajo releases_dir."""
        self.ensure_dirs()
        for entry in os.listdir(self.cfg.releases_dir):
            if entry.endswith(".part"):
                victim = os.path.join(self.cfg.releases_dir, entry)
                if os.path.isdir(victim):
                    shutil.rmtree(victim, ignore_errors=True)
                else:
                    try:
                        os.remove(victim)
                    except FileNotFoundError:
                        pass

    # ── instalación atómica ───────────────────────────────────────────────────
    def install_atomic(self, version, tarball_bytes):
        """Extrae el tarball a un temp en el MISMO FS y lo renombra a releases/<version>.

        Si la release ya existe (reintento idempotente), no re-extrae.
        """
        self.ensure_dirs()
        dest = self.release_path(version)
        if os.path.isdir(dest) and os.path.exists(os.path.join(dest, "bundle-manifest.json")):
            return dest  # ya instalada

        # Temp en releases_dir (mismo FS) con sufijo .part para que discard_part_files lo
        # limpie si el proceso muere a mitad.
        staging = tempfile.mkdtemp(prefix=f".{version}-", suffix=".part",
                                   dir=self.cfg.releases_dir)
        try:
            self._extract_tar(tarball_bytes, staging)
            # Renombre atómico (mismo FS). Si dest existe (parcial), reemplázalo.
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            os.replace(staging, dest)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError(f"fallo al instalar {version}: {exc}") from exc
        return dest

    @staticmethod
    def _extract_tar(tarball_bytes, dest_dir):
        import io

        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tf:
            members = tf.getmembers()
            _reject_unsafe_members(members, dest_dir)
            # Aplana el directorio raíz del tarball (cam-counter-edge-<ver>/...) en dest_dir.
            top = _common_top(members)
            for m in members:
                name = m.name
                if top and (name == top or name.startswith(top + "/")):
                    m.name = name[len(top) + 1:] if name != top else "."
                if m.name in ("", "."):
                    continue
                _safe_extract_member(tf, m, dest_dir)

    # ── activación / rollback atómicos (symlink swap) ─────────────────────────
    def activate(self, version):
        """Apunta `current` -> releases/<version> mediante symlink atómico (os.replace)."""
        dest = self.release_path(version)
        if not os.path.isdir(dest):
            raise InstallError(f"no se puede activar {version}: {dest} no existe")
        link = self.cfg.current_link
        # Symlink RELATIVO (releases/<version>) para portabilidad del install_root.
        rel_target = os.path.join("releases", version)
        tmp_link = link + ".new"
        if os.path.islink(tmp_link) or os.path.exists(tmp_link):
            os.remove(tmp_link)
        os.symlink(rel_target, tmp_link)
        os.replace(tmp_link, link)  # swap atómico en el MISMO directorio (install_root)

    def rollback(self, to_version):
        """Re-apunta `current` a `to_version` (la last_good). Atómico."""
        self.activate(to_version)

    # ── retención (nunca borrar last_good ni current) ─────────────────────────
    def prune_old_releases(self):
        keep = max(self.cfg.min_releases_retained, 2)
        protected = set()
        cur = self.current_version()
        lg = self.last_good()
        if cur:
            protected.add(cur)
        if lg:
            protected.add(lg)

        entries = [
            e for e in os.listdir(self.cfg.releases_dir)
            if os.path.isdir(self.release_path(e)) and not e.endswith(".part")
        ]
        # Orden estable por mtime (más nuevas al final).
        entries.sort(key=lambda e: os.path.getmtime(self.release_path(e)))

        removable = [e for e in entries if e not in protected]
        # Conserva al menos `keep` releases en total: borra sólo el exceso, más antiguas.
        excess = len(entries) - keep
        for victim in removable:
            if excess <= 0:
                break
            shutil.rmtree(self.release_path(victim), ignore_errors=True)
            excess -= 1


def _common_top(members):
    tops = set()
    for m in members:
        name = m.name.strip("/")
        if not name:
            continue
        tops.add(name.split("/", 1)[0])
    return next(iter(tops)) if len(tops) == 1 else None


def _reject_unsafe_members(members, dest_dir):
    for m in members:
        if m.issym() or m.islnk():
            # Rechaza enlaces que apunten fuera (defensa ante tarballs maliciosos).
            target = m.linkname
            if os.path.isabs(target) or ".." in target.split("/"):
                raise InstallError(f"enlace inseguro en el tarball: {m.name} -> {target}")
        if os.path.isabs(m.name) or ".." in m.name.split("/"):
            raise InstallError(f"ruta insegura en el tarball: {m.name}")


def _safe_extract_member(tf, member, dest_dir):
    target = os.path.realpath(os.path.join(dest_dir, member.name))
    if not (target == os.path.realpath(dest_dir)
            or target.startswith(os.path.realpath(dest_dir) + os.sep)):
        raise InstallError(f"path traversal bloqueado: {member.name}")
    tf.extract(member, dest_dir, set_attrs=False)
