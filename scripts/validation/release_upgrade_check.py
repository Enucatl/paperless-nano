"""Verify retained-image upgrade, legacy quarantine, and paired-state backout.

Run in a disposable current-image controller with Docker CLI/socket, the source
at /source, a private /evidence output volume, and host /tmp mounted at /tmp.
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

PREFIX = "paperless-release-upgrade"
OUT = Path("/evidence")
RUNTIME = Path("/tmp/paperless-release-upgrade")


def docker(*args: str, data: bytes | None = None) -> bytes:
    """Keep command failures private and avoid printing mounted secret values."""
    result = subprocess.run(
        ["docker", *args], input=data, capture_output=True, check=False
    )
    if result.returncode:
        (OUT / "private-upgrade-error.log").write_bytes(result.stdout + result.stderr)
        raise RuntimeError(f"Docker {args[0]} failed; inspect private upgrade evidence")
    return result.stdout


def main() -> None:
    """Migrate a seeded retained deployment, then restore its paired old state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--baseline", default="paperless-release-baseline-core:retained"
    )
    args = parser.parse_args()
    assert not RUNTIME.exists(), (
        "Remove only a previously inspected abandoned upgrade runtime"
    )
    OUT.mkdir(exist_ok=True)
    RUNTIME.mkdir(mode=0o700)
    names = {role: PREFIX + "-" + role for role in ("db", "data", "media")}
    for name in ("db_password", "secret_key"):
        path = RUNTIME / name
        path.write_text(secrets.token_hex(24))
        path.chmod(0o444)
    env = {
        "PAPERLESS_DBENGINE": "postgresql",
        "PAPERLESS_DBHOST": "db",
        "PAPERLESS_DBNAME": "paperless",
        "PAPERLESS_DBUSER": "paperless",
        "PAPERLESS_REDIS": "redis://broker:6379",
        "PAPERLESS_DATA_DIR": "/usr/src/paperless/data",
        "PAPERLESS_MEDIA_ROOT": "/usr/src/paperless/media",
        "PAPERLESS_CONSUMPTION_DIR": "/tmp/consume",
        "PAPERLESS_DB_TIMEOUT": "60",
        "PAPERLESS_DISABLE_DBHANDLER": "true",
    }
    config = {
        "services": {
            "db": {
                "image": "postgres:18",
                "userns_mode": "host",
                "environment": {
                    "POSTGRES_DB": "paperless",
                    "POSTGRES_USER": "paperless",
                    "POSTGRES_PASSWORD_FILE": "/run/secrets/db_password",
                },
                "secrets": ["db_password"],
                "volumes": [names["db"] + ":/var/lib/postgresql"],
            },
            "broker": {"image": "docker.io/library/redis:8", "userns_mode": "host"},
            "app": {
                "image": args.baseline,
                "userns_mode": "host",
                "environment": env,
                "secrets": ["db_password", "secret_key"],
                "volumes": [
                    names["data"] + ":/usr/src/paperless/data",
                    names["media"] + ":/usr/src/paperless/media",
                    os.environ["QUALIFICATION_SOURCE"] + ":/source:ro",
                ],
            },
        },
        "volumes": {v: {"name": v} for v in names.values()},
        "secrets": {
            name: {"file": str(RUNTIME / name)}
            for name in ("db_password", "secret_key")
        },
    }
    compose_path = RUNTIME / "compose.json"

    def compose(*command: str, data: bytes | None = None) -> bytes:
        """Execute one bounded command against only the temporary project."""
        compose_path.write_text(json.dumps(config))
        return docker(
            "compose", "-p", PREFIX, "-f", str(compose_path), *command, data=data
        )

    def manage(*command: str) -> bytes:
        """Read Compose secrets inside the helper and invoke Django management."""
        wrapper = 'export PAPERLESS_DBPASS="$(cat /run/secrets/db_password)" PAPERLESS_SECRET_KEY="$(cat /run/secrets/secret_key)"; mkdir -p /tmp/consume; exec python manage.py "$@"'
        return compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/bin/sh",
            "app",
            "-ec",
            wrapper,
            "sh",
            *command,
        )

    try:
        compose("up", "-d", "db", "broker")
        for _ in range(60):
            try:
                compose("exec", "-T", "db", "pg_isready", "-U", "paperless")
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Upgrade PostgreSQL unavailable")
        manage("migrate", "--noinput")
        seed = """
from hashlib import sha256
from pathlib import Path
from django.utils import timezone
from documents.models import Document,PaperlessTask,Tag,CustomField,CustomFieldInstance
from django.contrib.auth.models import User
user=User.objects.create_superuser(username="retained-admin",password=None,email="retained@example.invalid")
tag=Tag.objects.create(name="retained-tag")
field=CustomField.objects.create(name="retained-field",data_type="string")
data=Path("/source/src/documents/tests/samples/documents/originals/0000005.pdf").read_bytes()
doc=Document.objects.create(title="Retained upgrade document",content="Retained upgrade content",mime_type="application/pdf",checksum=sha256(data).hexdigest(),created=timezone.now(),owner=user,original_filename="retained.pdf")
doc.filename=f"{doc.pk:07}.pdf";doc.save(update_fields=["filename"])
doc.source_path.parent.mkdir(parents=True,exist_ok=True);doc.source_path.write_bytes(data)
doc.tags.add(tag)
CustomFieldInstance.objects.create(document=doc,field=field,value_text="preserved-value")
PaperlessTask.objects.create(task_id="retained-success",task_type="consume_file",status="success",date_created=timezone.now(),date_done=timezone.now())
PaperlessTask.objects.create(task_id="retained-pending",task_type="consume_file",status="pending",date_created=timezone.now())
print("RETAINED",doc.pk,sha256(data).hexdigest())
"""
        (OUT / "upgrade-seed.log").write_bytes(manage("shell", "-c", seed))
        (OUT / "pre-upgrade.dump").write_bytes(
            compose(
                "exec", "-T", "db", "pg_dump", "-U", "paperless", "-Fc", "paperless"
            )
        )
        (OUT / "pre-upgrade-files.tar").write_bytes(
            compose(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "tar",
                "app",
                "-C",
                "/usr/src/paperless",
                "-cf",
                "-",
                "data",
                "media",
            )
        )
        config["services"]["app"]["image"] = args.image
        manage("migrate", "documents", "0030", "--noinput")
        legacy = """
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
apps=MigrationExecutor(connection).loader.project_state([("documents","0030_processor_permissions")]).apps
Job=apps.get_model("documents","DocumentJob")
Job.objects.create(kind="reprocess",status="waiting",timeout=1800,input={"document_id":1},result={"invocations":{"parser":{"status":"leased","version":1,"token":"retained-lease","result":{"retained":True}}}})
Job.objects.create(kind="reprocess",status="succeeded",timeout=1800,input={"document_id":1},result={"retained":"completed"})
"""
        manage("shell", "-c", legacy)
        manage("migrate", "--noinput")
        verify = """
from hashlib import sha256
from documents.models import Document,PaperlessTask,CustomFieldInstance,DocumentJob
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
doc=Document.objects.get(title="Retained upgrade document")
assert sha256(doc.source_path.read_bytes()).hexdigest()==doc.checksum
assert doc.content=="Retained upgrade content"
assert list(doc.tags.values_list("name",flat=True))==["retained-tag"]
assert CustomFieldInstance.objects.get(document=doc).value_text=="preserved-value"
assert PaperlessTask.objects.get(task_id="retained-success").status=="success"
assert PaperlessTask.objects.get(task_id="retained-pending").status=="pending"
job=DocumentJob.objects.get(status="attention")
assert job.result["invocations"]["parser"]["token"]=="retained-lease"
assert "Legacy operation lifecycle" in job.error
assert DocumentJob.objects.get(status="succeeded").result=={"retained":"completed"}
assert not MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes())
print("Upgrade preserves document/files/metadata/history; unfinished legacy journal quarantined without replay")
"""
        (OUT / "upgrade-verification.log").write_bytes(manage("shell", "-c", verify))
        manage("makemigrations", "--check", "--dry-run", "--skip-checks")
        compose("stop", "db")
        compose("rm", "-f", "db")
        for role in ("db", "data", "media"):
            old_name = names[role]
            new_name = PREFIX + "-backout-" + role
            config["volumes"][new_name] = {"name": new_name}
            config["services"]["db" if role == "db" else "app"]["volumes"] = [
                volume.replace(old_name + ":", new_name + ":")
                for volume in config["services"]["db" if role == "db" else "app"][
                    "volumes"
                ]
            ]
        config["services"]["app"]["image"] = args.baseline
        compose("up", "-d", "db")
        for _ in range(60):
            try:
                compose("exec", "-T", "db", "pg_isready", "-U", "paperless")
                break
            except RuntimeError:
                time.sleep(1)
        compose(
            "exec",
            "-T",
            "db",
            "pg_restore",
            "-U",
            "paperless",
            "-d",
            "paperless",
            "--exit-on-error",
            data=(OUT / "pre-upgrade.dump").read_bytes(),
        )
        compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "tar",
            "app",
            "-C",
            "/usr/src/paperless",
            "-xf",
            "-",
            data=(OUT / "pre-upgrade-files.tar").read_bytes(),
        )
        backout_verify = (
            verify.split('job=DocumentJob.objects.get(status="attention")', maxsplit=1)[
                0
            ]
            .replace(",DocumentJob", "")
            .replace("from django.db import connection", "")
            .replace("from django.db.migrations.executor import MigrationExecutor", "")
            + '\nprint("Old image reads restored paired pre-upgrade state")\n'
        )
        (OUT / "backout-verification.log").write_bytes(
            manage("shell", "-c", backout_verify)
        )
        result = {
            "baseline_image": json.loads(docker("image", "inspect", args.baseline))[0][
                "Id"
            ],
            "candidate_image": json.loads(docker("image", "inspect", args.image))[0][
                "Id"
            ],
            "baseline_migration_and_seed": True,
            "candidate_upgrade": True,
            "original_files_and_unrelated_metadata": True,
            "completed_and_pending_receipts_preserved": True,
            "legacy_leased_job_quarantined": True,
            "paired_old_image_backout": True,
            "migration_drift": False,
            "scope": "Retained supported deployment with synthetic stored document/metadata/task history, plus pre-0031 legacy job injection. Does not claim old pending Celery messages executed or reverse migrations are safe.",
        }
        (OUT / "upgrade-results.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    finally:
        compose("down", "--volumes", "--remove-orphans")
        shutil.rmtree(RUNTIME)


if __name__ == "__main__":
    main()
