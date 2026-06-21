#!/usr/bin/env python3
"""`fleet-status` — enumera un canal vía GSI1 del device-registry y reporta drift.

Para cada dispositivo del canal muestra `desired_version` (espejo que escribe la nube) vs
`reported_version` (lo que la Pi reporta), su `status`/`last_update_status` y si está en
drift (desired != reported). Lo usa, entre otros, el workflow `promote.yml` para EXIGIR que
los devices canary estén `healthy` en la versión candidata antes de promover a stable.

Sólo LECTURA del registry (Query del GSI1 `CHANNEL#<channel>`). No es la fuente de la versión
deseada del agente (que es el manifiesto S3); aquí el registry es observabilidad de flota.

`--mock <file.json>` permite correr en CI sin AWS (lista de items DynamoDB-JSON).
"""
import argparse
import json
import sys


def _attr(item, name, default=None):
    """Extrae un atributo DynamoDB-JSON (S/N/L/BOOL) a valor Python plano."""
    v = item.get(name)
    if v is None:
        return default
    if "S" in v:
        return v["S"]
    if "N" in v:
        return int(v["N"]) if v["N"].isdigit() else float(v["N"])
    if "BOOL" in v:
        return v["BOOL"]
    if "L" in v:
        return [_attr({"x": e}, "x") for e in v["L"]]
    return default


def query_channel(ddb, table, channel, gsi1="GSI1"):
    """Devuelve la lista de items (DynamoDB-JSON) del canal vía GSI1."""
    items = []
    kwargs = {
        "TableName": table,
        "IndexName": gsi1,
        "KeyConditionExpression": "GSI1PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"CHANNEL#{channel}"}},
    }
    while True:
        resp = ddb.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" in resp:
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        else:
            break
    return items


def summarize(items):
    """Convierte items en filas legibles + agrega drift/healthy."""
    rows = []
    for it in items:
        desired = _attr(it, "desired_version")
        reported = _attr(it, "reported_version")
        rows.append({
            "device_id": _attr(it, "device_id") or _attr(it, "PK", "").replace("DEVICE#", ""),
            "desired_version": desired,
            "reported_version": reported,
            "status": _attr(it, "status"),
            "last_update_status": _attr(it, "last_update_status"),
            "last_good_version": _attr(it, "last_good_version"),
            "drift": desired != reported,
        })
    return rows


def all_healthy_on(rows, version):
    """True sii todos los devices reportan `version` y last_update_status=healthy."""
    if not rows:
        return False
    return all(
        r["reported_version"] == version and r["last_update_status"] == "healthy"
        for r in rows
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, choices=["canary", "stable"])
    parser.add_argument("--table", default="cam-counter-devices")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--gsi1", default="GSI1")
    parser.add_argument("--require-healthy-version",
                        help="exit 1 si NO todos los devices están healthy en esta versión.")
    parser.add_argument("--mock", help="JSON con una lista de items DynamoDB-JSON (CI sin AWS).")
    parser.add_argument("--json", action="store_true", help="salida JSON.")
    args = parser.parse_args(argv)

    if args.mock:
        with open(args.mock, encoding="utf-8") as fh:
            items = json.load(fh)
    else:
        import boto3

        ddb = boto3.client("dynamodb", region_name=args.region)
        items = query_channel(ddb, args.table, args.channel, args.gsi1)

    rows = summarize(items)

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"canal={args.channel} dispositivos={len(rows)}")
        for r in rows:
            flag = "DRIFT" if r["drift"] else "ok"
            print(f"  {r['device_id']:<20} desired={r['desired_version']} "
                  f"reported={r['reported_version']} status={r['status']} "
                  f"upd={r['last_update_status']} [{flag}]")

    if args.require_healthy_version:
        if not all_healthy_on(rows, args.require_healthy_version):
            print(f"GATE FAIL: no todos los devices canary están healthy en "
                  f"{args.require_healthy_version}", file=sys.stderr)
            return 1
        print(f"GATE OK: todos healthy en {args.require_healthy_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
