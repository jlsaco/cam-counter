"""publish_manifest: sequence monótono + If-Match single-writer + repoint + mirror registry."""
import importlib.util
import io
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_publish():
    path = os.path.join(_REPO_ROOT, "scripts", "publish_manifest.py")
    spec = importlib.util.spec_from_file_location("publish_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = _load_publish()


class FakeS3:
    def __init__(self):
        self.store = {}
        self._n = 0

    def _etag(self):
        self._n += 1
        return f'"etag{self._n}"'

    def _nosuchkey(self):
        e = Exception("NoSuchKey")
        e.response = {"Error": {"Code": "NoSuchKey"}}
        return e

    def _precondition(self):
        e = Exception("PreconditionFailed")
        e.response = {"Error": {"Code": "PreconditionFailed"}}
        return e

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise self._nosuchkey()
        body, etag = self.store[Key]
        return {"Body": io.BytesIO(body), "ETag": etag}

    def head_object(self, Bucket, Key):
        if Key not in self.store:
            raise self._nosuchkey()
        body, etag = self.store[Key]
        return {"ContentLength": len(body), "ETag": etag}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **kw):
        existing = self.store.get(Key)
        if IfNoneMatch == "*" and existing is not None:
            raise self._precondition()
        if IfMatch is not None and (existing is None or existing[1] != IfMatch):
            raise self._precondition()
        etag = self._etag()
        body = Body if isinstance(Body, bytes) else Body.encode()
        self.store[Key] = (body, etag)
        return {"ETag": etag}


class FakeDDB:
    def __init__(self, items):
        self.items = items
        self.updates = []

    def query(self, **kw):
        return {"Items": self.items}

    def update_item(self, **kw):
        self.updates.append(kw)


def _artifact(version="0.1.0"):
    return {
        "key": f"releases/{version}/cam-counter-edge-{version}-arm64.tar.gz",
        "sha256": "a" * 64, "size_bytes": 100,
        "sig_key": f"releases/{version}/cam-counter-edge-{version}-arm64.tar.gz.minisig",
    }


def test_first_publish_sequence_one_previous_null():
    s3 = FakeS3()
    manifest, info = pm.publish(s3, "b", "canary", "0.1.0", _artifact("0.1.0"), None,
                                git_sha="abc", released_by="t", min_agent_version="0.1.0")
    assert manifest["sequence"] == 1
    assert manifest["previous_version"] is None
    # Validó contra el schema dentro de publish().


def test_second_publish_increments_sequence_and_sets_previous():
    s3 = FakeS3()
    pm.publish(s3, "b", "canary", "0.1.0", _artifact("0.1.0"), None,
               git_sha="abc", released_by="t", min_agent_version="0.1.0")
    manifest, _ = pm.publish(s3, "b", "canary", "0.2.0", _artifact("0.2.0"), None,
                             git_sha="def", released_by="t", min_agent_version="0.1.0")
    assert manifest["sequence"] == 2
    assert manifest["previous_version"] == "0.1.0"


def test_put_manifest_if_match_rejects_stale_etag():
    s3 = FakeS3()
    pm.put_manifest(s3, "b", "canary", {"x": 1}, etag=None)  # crea (If-None-Match=*)
    with pytest.raises(Exception, match="Precondition"):
        pm.put_manifest(s3, "b", "canary", {"x": 2}, etag='"stale"')


def test_create_only_if_none_match_rejects_overwrite():
    s3 = FakeS3()
    pm.put_manifest(s3, "b", "canary", {"x": 1}, etag=None)
    # Un segundo create-only sobre una clave existente -> rechazado (single-writer).
    with pytest.raises(Exception, match="Precondition"):
        pm.put_manifest(s3, "b", "canary", {"x": 2}, etag=None)


def test_repoint_reconstructs_artifact_from_published_version():
    s3 = FakeS3()
    art_key, sig_key, sha_key = pm.artifact_keys_for("0.3.0")
    s3.store[art_key] = (b"X" * 321, '"e"')
    s3.store[sha_key] = (f"{'b' * 64}  cam-counter-edge-0.3.0-arm64.tar.gz\n".encode(), '"e"')
    art = pm.reconstruct_artifact(s3, "b", "0.3.0")
    assert art["key"] == art_key
    assert art["sig_key"] == sig_key
    assert art["sha256"] == "b" * 64
    assert art["size_bytes"] == 321


def test_mirror_desired_version_updates_all_channel_devices():
    ddb = FakeDDB([
        {"PK": {"S": "DEVICE#rpi-001"}},
        {"PK": {"S": "DEVICE#rpi-002"}},
    ])
    n = pm.mirror_desired_version(ddb, "cam-counter-devices", "canary", "0.2.0")
    assert n == 2
    assert all(u["ExpressionAttributeValues"][":v"]["S"] == "0.2.0" for u in ddb.updates)


def test_publish_mirrors_registry():
    s3 = FakeS3()
    ddb = FakeDDB([{"PK": {"S": "DEVICE#rpi-001"}}])
    _, info = pm.publish(s3, "b", "canary", "0.1.0", _artifact("0.1.0"), None,
                         git_sha="abc", released_by="t", min_agent_version="0.1.0",
                         ddb=ddb, devices_table="cam-counter-devices")
    assert info["mirrored"] == 1
