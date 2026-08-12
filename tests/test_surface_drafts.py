from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from enso import surface_drafts
from enso.outbound import AppHomePublication, CanvasPublication, HomeHeaderBlock
from enso.surface_drafts import ChannelCanvasTarget, SurfaceDraftOrigin

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _origin() -> SurfaceDraftOrigin:
    return SurfaceDraftOrigin(
        account_id="T-account",
        route_id="slack.channel.C-origin",
        route_kind="channel",
        workspace_id="reports",
        access_profile="publisher",
        route_audit=True,
        user_id="U-requester",
        channel_id="C-origin",
        thread_ts="100.200",
        conversation_type="channel",
        audit_turn_id="turn-123",
    )


def _selectors(**overrides: str) -> dict[str, str]:
    selectors = {
        "account_id": "T-account",
        "user_id": "U-requester",
        "channel_id": "C-origin",
        "message_ts": "300.400",
    }
    selectors.update(overrides)
    return selectors


def _channel_target(
    *,
    operation: str = "create",
    canvas_id: str | None = None,
    title: str | None = None,
    permalink: str | None = None,
    edit_timestamp: int | None = None,
) -> ChannelCanvasTarget:
    return ChannelCanvasTarget(
        operation=operation,
        canvas_id=canvas_id,
        title=title,
        permalink=permalink,
        edit_timestamp=edit_timestamp,
    )


def _row(tmp_enso: Any, draft_id: str) -> sqlite3.Row:
    with sqlite3.connect(Path(tmp_enso) / "enso.db") as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM _enso_surface_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
    assert row is not None
    return row


def _create_bound(publication: Any | None = None) -> Any:
    publication = publication or CanvasPublication(
        title="Incident review",
        markdown="# Incident review\n\n- Owner: Ada",
        fallback_text="Incident review",
        placement="standalone",
    )
    draft = surface_drafts.create(
        publication,
        source_text="Create a persistent incident review",
        origin=_origin(),
        ttl_seconds=300,
    )
    assert surface_drafts.bind_message(draft.draft_id, message_ts="300.400") is True
    return draft


@pytest.mark.parametrize(
    "publication",
    [
        CanvasPublication(
            title="Incident review",
            markdown="# Incident review\n\n- Owner: Ada",
            fallback_text="Incident review",
            placement="standalone",
        ),
        AppHomePublication(
            blocks=(HomeHeaderBlock(text="Weekly briefing"),),
            fallback_text="Weekly briefing",
        ),
    ],
)
def test_create_bind_claim_round_trips_publication_and_origin(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
    publication: Any,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = surface_drafts.create(
        publication,
        source_text="Please publish this persistent surface",
        origin=_origin(),
        ttl_seconds=300,
    )

    assert draft.publication == publication
    assert draft.source_text == "Please publish this persistent surface"
    assert draft.origin == _origin()
    assert draft.status == "pending"
    assert surface_drafts.bind_message(draft.draft_id, message_ts="300.400") is True

    claimed = surface_drafts.claim(
        draft.draft_id,
        action="publish",
        **_selectors(),
    )

    assert claimed is not None
    assert claimed.publication == publication
    assert claimed.source_text == "Please publish this persistent surface"
    assert claimed.origin == _origin()
    assert claimed.message_ts == "300.400"
    assert claimed.status == "publishing"


def test_channel_canvas_target_round_trips_and_is_bound_to_claim(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    publication = CanvasPublication(
        title="New operating plan",
        markdown="# New operating plan",
        fallback_text="Replace the operating plan Canvas.",
        placement="channel",
    )
    target = _channel_target(
        operation="replace",
        canvas_id="F-existing",
        title="Old operating plan",
        permalink="https://example.slack.com/docs/F-existing",
        edit_timestamp=100,
    )

    draft = surface_drafts.create(
        publication,
        source_text="Replace the channel Canvas",
        origin=_origin(),
        channel_canvas_target=target,
    )
    assert draft.channel_canvas_target == target
    assert surface_drafts.bind_message(draft.draft_id, message_ts="300.400")

    claimed = surface_drafts.claim(
        draft.draft_id,
        action="publish",
        **_selectors(),
    )

    assert claimed is not None
    assert claimed.channel_canvas_target == target
    row = _row(tmp_enso, draft.draft_id)
    assert row["target_json"] is not None
    assert row["target_hash"] is not None


def test_channel_canvas_draft_requires_a_trusted_target(
    tmp_enso: Any,
) -> None:
    publication = CanvasPublication(
        title="Plan",
        markdown="# Plan",
        fallback_text="Plan",
        placement="channel",
    )

    with pytest.raises(ValueError, match="channel Canvas target"):
        surface_drafts.create(
            publication,
            source_text="Create or replace the channel Canvas",
            origin=_origin(),
        )


def test_changed_channel_canvas_target_fails_integrity_check_and_scrubs_content(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    publication = CanvasPublication(
        title="Plan",
        markdown="# Plan",
        fallback_text="Plan",
        placement="channel",
    )
    draft = surface_drafts.create(
        publication,
        source_text="Replace the channel Canvas",
        origin=_origin(),
        channel_canvas_target=_channel_target(
            operation="replace",
            canvas_id="F-original",
            title="Original",
            permalink="https://example.slack.com/docs/F-original",
            edit_timestamp=100,
        ),
    )
    assert surface_drafts.bind_message(draft.draft_id, message_ts="300.400")
    with sqlite3.connect(Path(tmp_enso) / "enso.db") as connection:
        connection.execute(
            "UPDATE _enso_surface_drafts SET target_json=? WHERE draft_id=?",
            (
                '{"canvas_id":"F-other","edit_timestamp":100,'
                '"operation":"replace","permalink":'
                '"https://example.slack.com/docs/F-other","title":"Other","version":1}',
                draft.draft_id,
            ),
        )

    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors()) is None
    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] == "failed"
    assert row["publication_json"] is None
    assert row["source_text"] is None
    assert row["target_json"] is None


def test_create_uses_unique_opaque_url_safe_ids(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    first = surface_drafts.create(
        CanvasPublication(
            title="Secret title",
            markdown="secret-body",
            fallback_text="Secret",
            placement="standalone",
        ),
        source_text="secret-source",
        origin=_origin(),
        ttl_seconds=300,
    )
    second = surface_drafts.create(
        CanvasPublication(
            title="Secret title",
            markdown="secret-body",
            fallback_text="Secret",
            placement="standalone",
        ),
        source_text="secret-source",
        origin=_origin(),
        ttl_seconds=300,
    )

    assert first.draft_id != second.draft_id
    for draft_id in (first.draft_id, second.draft_id):
        assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", draft_id)
        assert len(draft_id) <= 200
        assert not any(
            value in draft_id
            for value in ("T-account", "U-requester", "C-origin", "secret-source")
        )


def test_confirmation_message_can_only_be_bound_once(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = surface_drafts.create(
        CanvasPublication(
            title="Plan",
            markdown="# Plan",
            fallback_text="Plan",
            placement="standalone",
        ),
        source_text="Create a plan",
        origin=_origin(),
        ttl_seconds=300,
    )

    assert surface_drafts.bind_message(draft.draft_id, message_ts="300.400") is True
    assert surface_drafts.bind_message(draft.draft_id, message_ts="attacker-message") is False
    assert _row(tmp_enso, draft.draft_id)["message_ts"] == "300.400"


@pytest.mark.parametrize(
    ("selector", "wrong_value"),
    [
        ("account_id", "T-other"),
        ("user_id", "U-other"),
        ("channel_id", "C-other"),
        ("message_ts", "999.999"),
    ],
)
def test_wrong_scope_cannot_claim_and_leaves_draft_pending(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
    wrong_value: str,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = _create_bound()

    claimed = surface_drafts.claim(
        draft.draft_id,
        action="publish",
        **_selectors(**{selector: wrong_value}),
    )

    assert claimed is None
    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] == "pending"
    assert row["publication_json"] is not None
    assert row["source_text"] is not None


def test_unbound_draft_cannot_be_claimed(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = surface_drafts.create(
        CanvasPublication(
            title="Plan",
            markdown="# Plan",
            fallback_text="Plan",
            placement="standalone",
        ),
        source_text="Create a plan",
        origin=_origin(),
        ttl_seconds=300,
    )

    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors()) is None
    assert _row(tmp_enso, draft.draft_id)["status"] == "pending"


def test_claim_accepts_just_before_ttl_and_expires_at_the_boundary(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: clock["now"])

    before_boundary = _create_bound()
    clock["now"] = NOW + timedelta(seconds=300) - timedelta(microseconds=1)
    assert (
        surface_drafts.claim(
            before_boundary.draft_id,
            action="publish",
            **_selectors(),
        )
        is not None
    )

    clock["now"] = NOW
    at_boundary = _create_bound()
    clock["now"] = NOW + timedelta(seconds=300)
    assert (
        surface_drafts.claim(at_boundary.draft_id, action="publish", **_selectors()) is None
    )
    row = _row(tmp_enso, at_boundary.draft_id)
    assert row["status"] == "expired"
    assert row["publication_json"] is None
    assert row["source_text"] is None


def test_publish_claim_is_one_time(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = _create_bound()

    first = surface_drafts.claim(draft.draft_id, action="publish", **_selectors())
    second = surface_drafts.claim(draft.draft_id, action="publish", **_selectors())

    assert first is not None
    assert second is None
    assert _row(tmp_enso, draft.draft_id)["status"] == "publishing"


def test_publish_and_cancel_race_has_exactly_one_winner(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = _create_bound()

    def attempt(action: str) -> Any:
        return surface_drafts.claim(draft.draft_id, action=action, **_selectors())

    with ThreadPoolExecutor(max_workers=2) as executor:
        publish = executor.submit(attempt, "publish")
        cancel = executor.submit(attempt, "cancel")
        results = (publish.result(), cancel.result())

    assert sum(result is not None for result in results) == 1
    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] in {"publishing", "cancelled"}
    if row["status"] == "cancelled":
        assert row["publication_json"] is None
        assert row["source_text"] is None
    else:
        assert row["publication_json"] is not None
        assert row["source_text"] is not None


def test_new_channel_draft_only_supersedes_same_requesters_pending_draft(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    publication = CanvasPublication(
        title="Channel report",
        markdown="# Channel report",
        fallback_text="Channel report",
        placement="channel",
    )
    first = surface_drafts.create(
        publication,
        source_text="First request",
        origin=_origin(),
        channel_canvas_target=_channel_target(),
    )
    assert surface_drafts.bind_message(first.draft_id, message_ts="300.400")

    surface_drafts.create(
        replace(publication, title="Other user's report"),
        source_text="Other request",
        origin=replace(_origin(), user_id="U-other"),
        channel_canvas_target=_channel_target(),
    )

    assert (
        surface_drafts.claim(first.draft_id, action="publish", **_selectors())
        is not None
    )


def test_new_same_user_channel_draft_supersedes_older_pending_draft(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    publication = CanvasPublication(
        title="Channel report",
        markdown="# Channel report",
        fallback_text="Channel report",
        placement="channel",
    )
    first = surface_drafts.create(
        publication,
        source_text="First request",
        origin=_origin(),
        channel_canvas_target=_channel_target(),
    )
    assert surface_drafts.bind_message(first.draft_id, message_ts="300.400")

    second = surface_drafts.create(
        replace(publication, title="Newer report"),
        source_text="Newer request",
        origin=_origin(),
        channel_canvas_target=_channel_target(),
    )
    assert surface_drafts.bind_message(second.draft_id, message_ts="500.600")

    assert surface_drafts.claim(first.draft_id, action="publish", **_selectors()) is None
    assert _row(tmp_enso, first.draft_id)["status"] == "superseded"


def test_unbound_replacement_does_not_supersede_working_confirmation(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    publication = CanvasPublication(
        title="Channel report",
        markdown="# Channel report",
        fallback_text="Channel report",
        placement="channel",
    )
    first = surface_drafts.create(
        publication,
        source_text="First request",
        origin=_origin(),
        channel_canvas_target=_channel_target(),
    )
    assert surface_drafts.bind_message(first.draft_id, message_ts="300.400")

    surface_drafts.create(
        replace(publication, title="Replacement that failed to post"),
        source_text="Replacement request",
        origin=_origin(),
        channel_canvas_target=_channel_target(),
    )

    assert (
        surface_drafts.claim(first.draft_id, action="publish", **_selectors())
        is not None
    )


def test_successful_channel_canvas_publish_supersedes_other_requesters_draft(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    publication = CanvasPublication(
        title="Channel report",
        markdown="# Channel report",
        fallback_text="Channel report",
        placement="channel",
    )
    target = _channel_target(
        operation="replace",
        canvas_id="FOLD",
        title="Old report",
        permalink="https://example.slack.com/docs/FOLD",
        edit_timestamp=100,
    )
    first = surface_drafts.create(
        publication,
        source_text="First request",
        origin=_origin(),
        channel_canvas_target=target,
    )
    second_origin = replace(_origin(), user_id="U-other")
    second = surface_drafts.create(
        replace(publication, title="Other report"),
        source_text="Other request",
        origin=second_origin,
        channel_canvas_target=target,
    )
    assert surface_drafts.bind_message(first.draft_id, message_ts="300.400")
    assert surface_drafts.bind_message(second.draft_id, message_ts="500.600")

    assert surface_drafts.claim(first.draft_id, action="publish", **_selectors())
    assert (
        surface_drafts.claim(
            second.draft_id,
            action="publish",
            account_id="T-account",
            user_id="U-other",
            channel_id="C-origin",
            message_ts="500.600",
        )
        is None
    )
    assert surface_drafts.finish(first.draft_id, status="published")
    assert _row(tmp_enso, second.draft_id)["status"] == "superseded"
    assert (
        surface_drafts.claim(
            second.draft_id,
            action="publish",
            account_id="T-account",
            user_id="U-other",
            channel_id="C-origin",
            message_ts="500.600",
        )
        is None
    )


def test_failed_channel_canvas_publish_releases_other_requesters_draft(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    publication = CanvasPublication(
        title="Channel report",
        markdown="# Channel report",
        fallback_text="Channel report",
        placement="channel",
    )
    target = _channel_target(operation="create")
    first = surface_drafts.create(
        publication,
        source_text="First request",
        origin=_origin(),
        channel_canvas_target=target,
    )
    second = surface_drafts.create(
        publication,
        source_text="Second request",
        origin=replace(_origin(), user_id="U-other"),
        channel_canvas_target=target,
    )
    assert surface_drafts.bind_message(first.draft_id, message_ts="300.400")
    assert surface_drafts.bind_message(second.draft_id, message_ts="500.600")
    assert surface_drafts.claim(first.draft_id, action="publish", **_selectors())

    assert surface_drafts.finish(first.draft_id, status="failed")

    assert _row(tmp_enso, second.draft_id)["status"] == "pending"
    assert surface_drafts.claim(
        second.draft_id,
        action="publish",
        account_id="T-account",
        user_id="U-other",
        channel_id="C-origin",
        message_ts="500.600",
    )
    assert _row(tmp_enso, second.draft_id)["status"] == "publishing"


def test_restart_reconcile_marks_interrupted_publish_unknown(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    interrupted = _create_bound()
    still_pending = _create_bound()
    assert (
        surface_drafts.claim(interrupted.draft_id, action="publish", **_selectors())
        is not None
    )

    surface_drafts.reconcile()

    interrupted_row = _row(tmp_enso, interrupted.draft_id)
    assert interrupted_row["status"] == "unknown"
    assert interrupted_row["publication_json"] is None
    assert interrupted_row["source_text"] is None
    assert _row(tmp_enso, still_pending.draft_id)["status"] == "pending"
    assert (
        surface_drafts.claim(interrupted.draft_id, action="publish", **_selectors()) is None
    )
    assert (
        surface_drafts.claim(still_pending.draft_id, action="publish", **_selectors())
        is not None
    )


def test_maintenance_expires_pending_without_releasing_active_publication(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: clock["now"])
    expired = _create_bound()
    publishing = _create_bound()
    assert (
        surface_drafts.claim(publishing.draft_id, action="publish", **_selectors())
        is not None
    )

    clock["now"] = NOW + timedelta(seconds=301)
    result = surface_drafts.maintain()

    assert result["expired"] == 1
    assert _row(tmp_enso, expired.draft_id)["status"] == "expired"
    active_row = _row(tmp_enso, publishing.draft_id)
    assert active_row["status"] == "publishing"
    assert active_row["publication_json"] is not None


def test_maintenance_leaves_unexpired_unbound_draft_available(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: clock["now"])
    draft = surface_drafts.create(
        CanvasPublication(
            title="Still staging",
            markdown="# Still staging",
            fallback_text="Still staging",
            placement="standalone",
        ),
        source_text="Create a Canvas",
        origin=_origin(),
        ttl_seconds=300,
    )

    clock["now"] = NOW + timedelta(seconds=299)
    assert surface_drafts.maintain() == {"expired": 0, "pruned": 0}

    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] == "pending"
    assert row["message_ts"] is None
    assert row["publication_json"] is not None


def test_maintenance_prunes_old_terminal_metadata(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: clock["now"])
    draft = _create_bound()
    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors())
    assert surface_drafts.finish(draft.draft_id, status="published")

    clock["now"] = NOW + timedelta(days=surface_drafts.RETENTION_DAYS, seconds=1)
    result = surface_drafts.maintain()

    assert result["pruned"] == 1
    with sqlite3.connect(Path(tmp_enso) / "enso.db") as connection:
        row = connection.execute(
            "SELECT 1 FROM _enso_surface_drafts WHERE draft_id=?",
            (draft.draft_id,),
        ).fetchone()
    assert row is None


def test_corrupt_publication_fails_closed_and_scrubs_content(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = _create_bound()
    with sqlite3.connect(Path(tmp_enso) / "enso.db") as connection:
        connection.execute(
            "UPDATE _enso_surface_drafts SET publication_json = ? WHERE draft_id = ?",
            ("{not valid json", draft.draft_id),
        )

    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors()) is None
    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] == "failed"
    assert row["publication_json"] is None
    assert row["source_text"] is None


def test_changed_valid_publication_fails_integrity_check_and_scrubs_content(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = _create_bound()
    replacement = CanvasPublication(
        title="Different report",
        markdown="# Different report",
        fallback_text="Different report",
        placement="standalone",
    )
    with sqlite3.connect(Path(tmp_enso) / "enso.db") as connection:
        connection.execute(
            "UPDATE _enso_surface_drafts SET publication_json = ? WHERE draft_id = ?",
            (
                surface_drafts.serialize_surface_publication(replacement),
                draft.draft_id,
            ),
        )

    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors()) is None
    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] == "failed"
    assert row["publication_json"] is None
    assert row["source_text"] is None


def test_origin_can_be_revalidated_before_corrupt_payload_is_claimed_and_scrubbed(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = _create_bound()
    with sqlite3.connect(Path(tmp_enso) / "enso.db") as connection:
        connection.execute(
            "UPDATE _enso_surface_drafts SET publication_json = ? WHERE draft_id = ?",
            ("{not valid json", draft.draft_id),
        )

    scope = surface_drafts.get_origin_scoped(draft.draft_id, **_selectors())
    assert scope is not None
    assert scope.origin == _origin()
    assert scope.status == "pending"
    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors()) is None
    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] == "failed"
    assert row["publication_json"] is None
    assert row["source_text"] is None


@pytest.mark.parametrize("terminal_status", ["published", "failed", "partial", "unknown"])
def test_finish_scrubs_sensitive_content_for_every_terminal_outcome(
    tmp_enso: Any,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    monkeypatch.setattr(surface_drafts, "_utc_now", lambda: NOW)
    draft = _create_bound()
    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors()) is not None

    surface_drafts.finish(draft.draft_id, status=terminal_status)

    row = _row(tmp_enso, draft.draft_id)
    assert row["status"] == terminal_status
    assert row["publication_json"] is None
    assert row["source_text"] is None
    assert surface_drafts.claim(draft.draft_id, action="publish", **_selectors()) is None
