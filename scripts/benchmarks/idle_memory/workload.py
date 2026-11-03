# ruff: noqa: INP001, T201
"""Bounded document workload for the isolated residency comparison."""

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode


def task_rows(response):
    """Accept the current paginated task response."""
    return response["results"] if isinstance(response, dict) else response


def wait_task(api, *, task_id=None, task_type=None, existing=(), timeout=300):
    """Wait for a new task to reach its recorded terminal outcome."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        suffix = f"&task_id={task_id}" if task_id else ""
        rows = task_rows(api("/api/tasks/?page_size=100" + suffix))
        for row in rows:
            if task_id and row["task_id"] != task_id:
                continue
            if task_type and (
                row.get("task_type") != task_type or row["task_id"] in existing
            ):
                continue
            status = row["status"].lower()
            if status in ("failure", "revoked"):
                raise RuntimeError(
                    f"Benchmark task ended with {status}; inspect private service log"
                )
            if status == "success":
                return {
                    "task_id": row["task_id"],
                    "status": status,
                    "task_type": row.get("task_type"),
                }
        time.sleep(1)
    raise RuntimeError("Benchmark task did not complete before deadline")


def ingest(api, reference, snapshot):
    """Upload a reserved representative PDF without altering its content."""
    data = (snapshot / reference["original"]).read_bytes()
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="benchmark-{reference["id"]}.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    start = time.monotonic()
    task_id = api(
        "/api/documents/post_document/",
        method="POST",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert isinstance(task_id, str)
    result = wait_task(api, task_id=task_id)
    result["admission_to_completion_seconds"] = time.monotonic() - start
    result["input_id"] = reference["id"]
    return result


def exercise(api, snapshot, output, phase, observe, idle_seconds):
    """Browse, suggest, update, ingest, reprocess and ingest again after idle."""
    seed = json.loads((snapshot / "results/seed-result.json").read_text())
    results = {}
    phase("browse-download")
    documents = api("/api/documents/?page_size=100")["results"]
    assert len(documents) == 12
    for document in documents:
        pk = document["id"]
        api(f"/api/documents/{pk}/download/?original=true", raw=True)
        api(f"/api/documents/{pk}/thumb/", raw=True)
    api("/api/documents/?query=created:2020&page_size=100")
    api("/api/statistics/")
    api("/api/status/")
    observe("post-browse-idle", 30)

    phase("first-metadata")
    first_id = documents[0]["id"]
    first = api(f"/api/documents/{first_id}/metadata/")
    assert isinstance(first["lang"], str)
    observe("post-first-metadata", 0)
    phase("metadata-detail-preview")
    metadata = {}
    for document in documents:
        pk = document["id"]
        api(f"/api/documents/{pk}/")
        metadata[pk] = api(f"/api/documents/{pk}/metadata/")
        api(f"/api/documents/{pk}/preview/", raw=True)
        assert isinstance(metadata[pk]["lang"], str)
    observe("post-all-metadata", 0)
    phase("cached-metadata")
    for document in documents:
        pk = document["id"]
        cached = api(f"/api/documents/{pk}/metadata/")
        assert {k: v for k, v in cached.items() if k != "lang"} == {
            k: v for k, v in metadata[pk].items() if k != "lang"
        }
    largest = max(metadata, key=lambda pk: metadata[pk]["original_size"] or 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        downloads = list(
            pool.map(
                lambda _: api(
                    f"/api/documents/{largest}/download/?original=true", raw=True
                ),
                range(2),
            )
        )
    assert downloads[0] == downloads[1]
    del downloads
    searchable = next(
        document
        for document in documents
        if re.search(r"\b[^\W\d_]{8,}\b", document.get("content") or "")
    )
    term = re.search(r"\b[^\W\d_]{8,}\b", searchable["content"]).group()
    matches = api(
        "/api/documents/?"
        + urlencode(
            {
                "query": "content:" + term,
                "include_selection_data": "true",
                "page_size": 100,
            }
        )
    )
    assert searchable["id"] in [document["id"] for document in matches["results"]]
    results["metadata_preview_concurrent_download_search"] = True
    results["metadata_languages"] = {
        str(pk): row["lang"] for pk, row in metadata.items()
    }
    observe("post-metadata-idle", idle_seconds)

    phase("cold-warm-suggestions")
    for document in documents:
        pk = document["id"]
        cold = api(f"/api/documents/{pk}/suggestions/")
        warm = api(f"/api/documents/{pk}/suggestions/")
        assert cold == warm
    observe("post-suggestions-idle", idle_seconds)

    phase("metadata-workflow")
    pk = seed["seeded_ids"][0]
    api(
        f"/api/documents/{pk}/", method="PATCH", body={"tags": [seed["trigger_tag_id"]]}
    )
    final = api(f"/api/documents/{pk}/")
    assert final["title"] == seed["workflow_final_title"]
    assert seed["final_tag_id"] in final["tags"]
    matches = api("/api/documents/?query=title:benchmarkfinalstate")["results"]
    assert pk in [document["id"] for document in matches]
    results["workflow_final_state_indexed"] = True

    phase("first-ingestion")
    results["first_ingestion"] = ingest(api, seed["reserved"][0], snapshot)
    assert api("/api/documents/?page_size=100")["count"] == 13

    phase("reprocess")
    existing = [row["task_id"] for row in task_rows(api("/api/tasks/?page_size=100"))]
    modest = next(
        row
        for row in documents
        if row.get("mime_type") == "application/pdf"
        and 1 <= (row.get("page_count") or 0) <= 10
    )
    api("/api/documents/reprocess/", method="POST", body={"documents": [modest["id"]]})
    results["reprocess"] = wait_task(
        api, task_type="reprocess_document", existing=existing
    )
    observe("post-processing-idle", idle_seconds)

    phase("ingestion-after-idle")
    results["ingestion_after_idle"] = ingest(api, seed["reserved"][1], snapshot)
    assert api("/api/documents/?page_size=100")["count"] == 14
    observe("final-idle", idle_seconds)
    phase("final-checks")
    results["scheduled_tasks"] = [
        {"task_type": row.get("task_type"), "status": row["status"]}
        for row in task_rows(api("/api/tasks/?page_size=100"))
        if row.get("trigger_source") == "scheduled"
    ]
    results["schedule_names"] = [
        row["name"] for row in api("/api/services/scheduler/")["jobs"]
    ]
    assert "Recover document jobs" in results["schedule_names"]
    (output / "workload.json").write_text(json.dumps(results, indent=2))
    print(json.dumps({"workload": results}), flush=True)


if __name__ == "__main__":
    assert task_rows({"results": [{"status": "success"}]}) == [{"status": "success"}]
    assert (
        wait_task(
            lambda path: {"results": [{"task_id": "test", "status": "SUCCESS"}]},
            task_id="test",
        )["status"]
        == "success"
    )
    print("workload task-response self-check passed")
