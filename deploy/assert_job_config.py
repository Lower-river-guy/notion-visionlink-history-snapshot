#!/usr/bin/env python3
"""Confirm a Cloud Run Job description matches the dry-run VisionLink snapshot.

Reads `gcloud run jobs describe --format=json` on stdin. Prints confirmation
lines and exits non-zero when the job would not stay read-only on the source
and append-only on the history database. Never prints secret values.
"""

from __future__ import annotations

import json
import sys

EXPECTED_SERVICE_ACCOUNT = (
    "visionlink-history-runner@work-projects-486912.iam.gserviceaccount.com"
)
EXPECTED_SECRET_ENV = "NOTION_TOKEN"
EXPECTED_SECRET = "Notion_Google_Cloud_Sync"
EXPECTED_SECRET_VERSION = "latest"
EXPECTED_SOURCE = "3db284de-cb43-80ed-9b6f-fc20d6cc20eb"
EXPECTED_DESTINATION = "0357c6bd-2650-4dfc-affb-72430beaca84"
EXPECTED_DESTINATION_TITLE = "Cat VisionLink History"
EXPECTED_TIMEZONE = "America/Los_Angeles"
EXPECTED_PLAIN = {
    "DRY_RUN": "true",
    "SOURCE_DATABASE_ID": EXPECTED_SOURCE,
    "DESTINATION_DATABASE_ID": EXPECTED_DESTINATION,
    "DESTINATION_DATABASE_TITLE": EXPECTED_DESTINATION_TITLE,
    "BUSINESS_TIMEZONE": EXPECTED_TIMEZONE,
}


class ConfigMismatch(Exception):
    pass


def _pod_specs(node):
    found = []
    if isinstance(node, dict):
        containers = node.get("containers")
        if isinstance(containers, list):
            found.append(node)
        for value in node.values():
            found.extend(_pod_specs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_pod_specs(item))
    return found


def _secret_ref(item: dict) -> tuple[str | None, str | None]:
    for source_key in ("valueSource", "valueFrom"):
        source = item.get(source_key) or {}
        if not isinstance(source, dict):
            continue
        ref = source.get("secretKeyRef") or {}
        if not isinstance(ref, dict):
            continue
        name = ref.get("secret") or ref.get("name")
        version = ref.get("version") or ref.get("key")
        if name:
            return name, version
    return None, None


def check(job: dict) -> None:
    specs = _pod_specs(job)
    if len(specs) != 1:
        raise ConfigMismatch(f"expected one container spec, found {len(specs)}")
    pod = specs[0]
    containers = pod.get("containers") or []
    if len(containers) != 1:
        raise ConfigMismatch(f"expected one container, found {len(containers)}")

    service_account = pod.get("serviceAccountName") or pod.get("serviceAccount")
    if service_account != EXPECTED_SERVICE_ACCOUNT:
        raise ConfigMismatch(
            "service account is "
            f"{service_account!r}, expected {EXPECTED_SERVICE_ACCOUNT}"
        )

    env = containers[0].get("env") or []
    plain = {}
    secrets = []
    for item in env:
        if not isinstance(item, dict) or "name" not in item:
            raise ConfigMismatch("container env entry is missing a name")
        name = item["name"]
        secret_name, secret_version = _secret_ref(item)
        if secret_name:
            if "value" in item:
                raise ConfigMismatch(f"{name} has both a literal value and a secret reference")
            secrets.append((name, secret_name, secret_version))
            continue
        if "value" not in item:
            raise ConfigMismatch(f"{name} has neither a literal value nor a secret reference")
        if name == EXPECTED_SECRET_ENV:
            raise ConfigMismatch(
                f"{EXPECTED_SECRET_ENV} must reference Secret Manager "
                f"{EXPECTED_SECRET}:{EXPECTED_SECRET_VERSION}"
            )
        plain[name] = item["value"]

    for key, expected in EXPECTED_PLAIN.items():
        if plain.get(key) != expected:
            raise ConfigMismatch(f"{key} is {plain.get(key)!r}, expected {expected!r}")

    if len(secrets) != 1 or secrets[0] != (
        EXPECTED_SECRET_ENV,
        EXPECTED_SECRET,
        EXPECTED_SECRET_VERSION,
    ):
        rendered = ", ".join(
            f"{name}={secret}:{version}" for name, secret, version in secrets
        ) or "(none)"
        raise ConfigMismatch(
            "secret mapping is "
            f"{rendered}, expected {EXPECTED_SECRET_ENV}={EXPECTED_SECRET}:{EXPECTED_SECRET_VERSION}"
        )


def main() -> int:
    try:
        job = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"ERROR: job description is not JSON ({exc})", file=sys.stderr)
        return 1
    try:
        check(job)
    except ConfigMismatch as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("confirmed: DRY_RUN=true")
    print(f"confirmed: {EXPECTED_SECRET_ENV}={EXPECTED_SECRET}:{EXPECTED_SECRET_VERSION}")
    print(f"confirmed: service account {EXPECTED_SERVICE_ACCOUNT}")
    print(f"confirmed: source {EXPECTED_SOURCE} read-only")
    print(f"confirmed: destination {EXPECTED_DESTINATION} append-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
