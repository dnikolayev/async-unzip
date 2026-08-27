"""Contracts for the Actions artifact lifecycle."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_artifacts_expire_after_consumers_finish():
    """Keep uploads bounded without deleting before the publish job finishes."""
    publish = (ROOT / ".github/workflows/publish.yml").read_text()
    assert publish.count("actions/upload-artifact@") == publish.count(
        "retention-days: 1"
    )

    cleanup = (ROOT / ".github/workflows/artifact-cleanup.yml").read_text()
    assert "workflows: [Publish to PyPI]" in cleanup
    assert "types: [completed]" in cleanup
    assert "workflow_run.conclusion == 'success'" in cleanup
    assert "/actions/runs/${RUN_ID}/artifacts?per_page=100" in cleanup
    assert cleanup.count('ids_file="$(mktemp)"') == 2
    assert cleanup.count("trap 'rm -f \"${ids_file}\"' EXIT") == 2
    assert "--jq '.artifacts[].id' >\"${ids_file}\"" in cleanup

    stale = cleanup.split("  delete-stale-artifacts:", maxsplit=1)[1]
    assert "1 day ago" in stale
    assert ".expired == false and .created_at < $cutoff" in stale
    assert '.id\' >"${ids_file}"' in stale
    assert ".workflow_run.id" not in stale
    assert "/actions/runs/${run_id}" not in stale
