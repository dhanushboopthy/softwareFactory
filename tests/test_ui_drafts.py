"""Drafts in the UI: write or upload, review, save, approve, build."""

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from factory.config import FactoryConfig
from factory.llm.base import LLMError, LLMProvider
from factory.report.models import Usage
from factory.stations.spec_writer import DraftResult
from factory.ui.app import create_app
from factory.ui.drafts import (
    DraftRunner,
    DraftStore,
    is_approved,
    safe_name,
    set_approved,
    text_hash,
)
from factory.ui.jobs import BuildRunner
from factory.ui.security import UiSettings

from .test_ui_builds import EXAMPLE, SCRIPTED, FakeSpawn
from .test_ui_security import BASE, StubEnvironment, settings_for

VALID = EXAMPLE.replace("approved: true", "approved: false")


class StubProvider(LLMProvider):
    name = "stub"
    model = "stub-1"

    def complete(self, **_: Any) -> Any:  # never called: draft_fn is replaced
        raise AssertionError


def scripted_draft(valid: bool = True, boom: Exception | None = None) -> Any:
    def draft(docs: Sequence[Path], out: Path, cfg: FactoryConfig, provider: LLMProvider,
              ) -> DraftResult:
        if boom is not None:
            raise boom
        usage = Usage(llm_calls=1, cost_usd=0.12)
        if valid:
            out.write_text(VALID)
            return DraftResult(out, 1, [], usage, ["no payments"])
        return DraftResult(None, 3, ["entities: missing"], usage, last_answer="app: half\n")
    return draft


def make(tmp_path: Path, draft_fn: Any = None, env: Mapping[str, str] = SCRIPTED,
         ) -> tuple[TestClient, UiSettings, DraftRunner, FakeSpawn]:
    settings = settings_for(tmp_path, env=dict(env))
    store = DraftStore(settings.cfg.drafts_dir / "_drafts")
    runner = DraftRunner(store, settings.cfg, settings.env,
                         draft_fn=draft_fn or scripted_draft(),
                         provider_fn=lambda _: StubProvider())
    spawn = FakeSpawn()
    builds = BuildRunner(settings, spawn=spawn)  # type: ignore[arg-type]
    client = TestClient(create_app(settings, StubEnvironment(settings), builds, runner),
                        base_url=BASE)
    return client, settings, runner, spawn


def post(client: TestClient, settings: UiSettings, path: str, **kwargs: Any) -> Any:
    data = {"csrf": settings.csrf_token, **kwargs.pop("data", {})}
    return client.post(path, data=data, follow_redirects=False, **kwargs)


def draft_id_of(response: Any) -> str:
    assert response.status_code == 303, response.text[:400]
    return str(response.headers["location"]).removeprefix("/drafts/")


def drafted(client: TestClient, settings: UiSettings, runner: DraftRunner) -> str:
    response = post(client, settings, "/drafts", data={"description": "a task tracker"})
    draft_id = draft_id_of(response)
    thread = runner._thread
    assert thread is not None
    thread.join(timeout=10)
    return draft_id


# --- pure helpers -------------------------------------------------------------------------------


def test_set_approved_edits_only_the_top_level_line() -> None:
    assert set_approved("approved: true\napp: x\n", False) == "approved: false\napp: x\n"
    assert set_approved("app: x\n", True) == "approved: true\napp: x\n"
    nested = "app: x\nentities:\n  A:\n    text: 'approved: yes'\n"
    assert set_approved(nested, True).count("approved") == 2
    assert is_approved("approved: true\n") and not is_approved("approved: false\n")
    assert not is_approved("app: x\n")


def test_safe_name_never_returns_a_path() -> None:
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("a b$c.md") == "a_b_c.md"
    assert safe_name("") == "document" and safe_name("...") == "document"


def test_the_store_rejects_hostile_ids(tmp_path: Path) -> None:
    store = DraftStore(tmp_path / "d")
    for bad in ("../x", "a/b", "", "a\n"):
        assert store.get(bad) is None


# --- drafting -----------------------------------------------------------------------------------


def test_a_description_becomes_a_ready_draft_with_its_gaps(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    draft_id = drafted(client, settings, runner)
    meta = runner.store.get(draft_id)
    assert meta is not None and meta.status == "ready" and meta.gaps == ["no payments"]
    assert meta.cost_usd == pytest.approx(0.12) and meta.provider == "stub"
    html = client.get(f"/drafts/{draft_id}").text
    assert "read all of this before approving" in html
    assert "Approve and start build" in html and "http-equiv" not in html


def test_a_failed_draft_keeps_the_last_answer_to_fix_by_hand(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path, scripted_draft(valid=False))
    draft_id = drafted(client, settings, runner)
    meta = runner.store.get(draft_id)
    assert meta is not None and meta.status == "failed" and meta.errors == ["entities: missing"]
    html = client.get(f"/drafts/{draft_id}").text
    assert "app: half" in html and "Approve and start build" not in html


@pytest.mark.parametrize("boom", [LLMError("rate limited"), RuntimeError("bug")])
def test_a_crash_while_drafting_is_reported_not_lost(tmp_path: Path, boom: Exception) -> None:
    client, settings, runner, _ = make(tmp_path, scripted_draft(boom=boom))
    draft_id = drafted(client, settings, runner)
    meta = runner.store.get(draft_id)
    assert meta is not None and meta.status == "failed" and str(boom) in meta.message


def test_the_page_reloads_while_drafting(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    meta = runner.store.create("generated", {"description.md": "x"})
    assert "http-equiv" in client.get(f"/drafts/{meta.id}").text


def test_no_input_oversize_and_no_provider_are_refused(tmp_path: Path) -> None:
    client, settings, _, _ = make(tmp_path)
    assert post(client, settings, "/drafts", data={"description": "  "}).status_code == 422
    huge = "x" * (settings.cfg.context_max_total_chars + 1)
    assert post(client, settings, "/drafts", data={"description": huge}).status_code == 413
    bare, bare_settings, _, _ = make(tmp_path / "bare", env={})
    response = post(bare, bare_settings, "/drafts", data={"description": "x"})
    assert response.status_code == 422 and "No LLM provider" in response.text


def test_a_second_draft_waits_for_the_first(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    first = runner.store.create("generated", {"description.md": "x"})
    gate = threading.Event()
    runner._draft = lambda *a, **k: (gate.wait(10), scripted_draft()(*a, **k))[1]
    runner.start(first.id)
    response = post(client, settings, "/drafts", data={"description": "again"})
    assert response.status_code == 409
    gate.set()
    assert runner._thread is not None
    runner._thread.join(timeout=10)


# --- uploading a file to review ------------------------------------------------------------------


def test_an_uploaded_file_is_opened_unapproved(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    response = post(client, settings, "/drafts",
                    files={"entities": ("mine.yaml", EXAMPLE, "text/yaml")})
    draft_id = draft_id_of(response)
    assert not is_approved(runner.store.read_text(draft_id))
    assert runner.store.get(draft_id).kind == "uploaded"  # type: ignore[union-attr]


def test_a_binary_upload_is_refused(tmp_path: Path) -> None:
    client, settings, _, _ = make(tmp_path)
    response = post(client, settings, "/drafts",
                    files={"entities": ("x.yaml", b"\xff\xfe\x00", "text/yaml")})
    assert response.status_code == 422


# --- save and approve -----------------------------------------------------------------------------


def ready_draft(client: TestClient, settings: UiSettings, runner: DraftRunner) -> str:
    return drafted(client, settings, runner)


def test_saving_never_approves(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    sneaky = VALID.replace("approved: false", "approved: true")
    response = post(client, settings, f"/drafts/{draft_id}/save", data={"text": sneaky})
    assert response.status_code == 200 and "Saved (not approved)" in response.text
    assert not is_approved(runner.store.read_text(draft_id))


def approve(client: TestClient, settings: UiSettings, runner: DraftRunner, draft_id: str,
            **overrides: str) -> Any:
    data = {"confirm": "on", "seen_sha": text_hash(runner.store.read_text(draft_id))}
    data.update(overrides)
    return post(client, settings, f"/drafts/{draft_id}/approve", data=data)


def test_approving_starts_a_build_from_the_approved_text(tmp_path: Path) -> None:
    client, settings, runner, spawn = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    response = approve(client, settings, runner, draft_id, no_llm="on")
    assert response.status_code == 303 and response.headers["location"].startswith("/runs/")
    run_id = response.headers["location"].removeprefix("/runs/")
    built = settings.cfg.drafts_dir / run_id / "entities.yaml"
    assert is_approved(built.read_text())
    assert not is_approved(runner.store.read_text(draft_id))  # the draft itself stays unapproved
    assert spawn.calls[0].argv[-1] == "--no-llm"
    meta = runner.store.get(draft_id)
    assert meta is not None and meta.build_run_id == run_id and meta.approved_at is not None


def test_approval_needs_the_box_ticked(tmp_path: Path) -> None:
    client, settings, runner, spawn = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    data = {"seen_sha": text_hash(runner.store.read_text(draft_id))}
    response = post(client, settings, f"/drafts/{draft_id}/approve", data=data)
    assert response.status_code == 422 and "Tick the box" in response.text
    assert not spawn.calls


def test_approval_refuses_text_the_reviewer_did_not_see(tmp_path: Path) -> None:
    client, settings, runner, spawn = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    response = approve(client, settings, runner, draft_id, seen_sha="0" * 64)
    assert response.status_code == 422 and "changed since" in response.text
    assert not spawn.calls


def test_an_invalid_file_cannot_be_approved(tmp_path: Path) -> None:
    client, settings, runner, spawn = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    post(client, settings, f"/drafts/{draft_id}/save", data={"text": "app: broken\n"})
    response = approve(client, settings, runner, draft_id)
    assert response.status_code == 422 and not spawn.calls


def test_a_failed_draft_cannot_be_approved_until_fixed(tmp_path: Path) -> None:
    client, settings, runner, spawn = make(tmp_path, scripted_draft(valid=False))
    draft_id = ready_draft(client, settings, runner)
    assert approve(client, settings, runner, draft_id).status_code == 409
    post(client, settings, f"/drafts/{draft_id}/save", data={"text": VALID})
    assert approve(client, settings, runner, draft_id).status_code == 303
    assert spawn.calls


def test_draft_routes_are_csrf_protected_and_404_on_unknown(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    for path in (f"/drafts/{draft_id}/save", f"/drafts/{draft_id}/approve", "/drafts"):
        assert client.post(path, data={}).status_code == 403
    for path in ("/drafts/nope", "/drafts/..%2Fx", "/drafts/nope/download"):
        assert client.get(path).status_code == 404


def test_the_dashboard_lists_drafts_and_the_file_downloads(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    assert draft_id in client.get("/").text
    download = client.get(f"/drafts/{draft_id}/download")
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    assert download.text == runner.store.read_text(draft_id)


def test_drafted_text_is_escaped(tmp_path: Path) -> None:
    client, settings, runner, _ = make(tmp_path)
    draft_id = ready_draft(client, settings, runner)
    evil = VALID.replace("gaps: []", "gaps: []") + "\n# </textarea><b>x</b>\n"
    post(client, settings, f"/drafts/{draft_id}/save", data={"text": evil})
    html = client.get(f"/drafts/{draft_id}").text
    assert "</textarea><b>x</b>" not in html and "&lt;/textarea&gt;" in html
