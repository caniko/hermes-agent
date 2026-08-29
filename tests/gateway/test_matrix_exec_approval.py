import types

import pytest
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig


class TestMatrixExecApprovalReactions:


    @pytest.mark.asyncio
    async def test_reaction_resolves_pending_approval(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@liizfq:liizfq.top")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
        # Resolve user_id so _is_self_sender doesn't defensively drop all traffic (#15763).
        adapter._user_id = "@bot:example.org"
        adapter._approval_prompts_by_event["$target"] = _MatrixApprovalPrompt(
            session_key="sess-1", chat_id="!room:example.org", message_id="$target"
        )
        adapter._approval_prompt_by_session["sess-1"] = "$target"

        content = {"m.relates_to": {"event_id": "$target", "key": "✅"}}
        event = types.SimpleNamespace(
            sender="@liizfq:liizfq.top",
            event_id="$react1",
            room_id="!room:example.org",
            content=content,
        )

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._on_reaction(event)

        mock_resolve.assert_called_once_with("sess-1", "once")
        assert "$target" not in adapter._approval_prompts_by_event
        assert "sess-1" not in adapter._approval_prompt_by_session

    @pytest.mark.asyncio
    async def test_reaction_from_unauthorized_room_cannot_approve(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@alice:example.org")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(
            PlatformConfig(
                enabled=True,
                token="tok",
                extra={"homeserver": "https://matrix.example.org"},
            )
        )
        adapter._user_id = "@bot:example.org"
        adapter._allowed_room_ids = {"!allowed:example.org"}
        adapter._approval_prompts_by_event["$target"] = _MatrixApprovalPrompt(
            session_key="sess-1",
            chat_id="!other:example.org",
            message_id="$target",
        )
        event = types.SimpleNamespace(
            sender="@alice:example.org",
            event_id="$react-other-room",
            room_id="!other:example.org",
            content={"m.relates_to": {"event_id": "$target", "key": "✅"}},
        )

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._on_reaction(event)

        mock_resolve.assert_not_called()
        assert adapter._approval_prompts_by_event["$target"].resolved is False

    @pytest.mark.asyncio
    async def test_different_authorized_user_cannot_approve_request(self, monkeypatch):
        monkeypatch.setenv(
            "MATRIX_ALLOWED_USERS",
            "@alice:example.org,@dejana:example.org",
        )
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(
            PlatformConfig(
                enabled=True,
                token="tok",
                extra={"homeserver": "https://matrix.example.org"},
            )
        )
        adapter._user_id = "@bot:example.org"
        adapter._allowed_room_ids = {"!room:example.org"}
        adapter._send_invalid_reaction_feedback = AsyncMock()
        adapter._approval_prompts_by_event["$target"] = _MatrixApprovalPrompt(
            session_key="sess-1",
            chat_id="!room:example.org",
            message_id="$target",
            requester_user_id="@alice:example.org",
        )
        event = types.SimpleNamespace(
            sender="@dejana:example.org",
            event_id="$react-dejana",
            room_id="!room:example.org",
            content={"m.relates_to": {"event_id": "$target", "key": "✅"}},
        )

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._on_reaction(event)

        mock_resolve.assert_not_called()
        assert adapter._approval_prompts_by_event["$target"].resolved is False
