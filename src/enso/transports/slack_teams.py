"""Static Slack routing for shared workspaces and native access profiles."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from .. import audit, ledger, policy
from ..core import ExecutionContext
from ..surface_drafts import SurfaceDraftOrigin
from ..teams import AccessProfile, Decision, Route, TeamsConfig, Workspace, load_teams, resolve

if TYPE_CHECKING:
    from ..core import Runtime
    from ..policy import Launch
    from ..surface_drafts import SurfaceDraftOrigin
    from .slack import SlackContext, SlackTransport

log = logging.getLogger(__name__)

CONFIG_ERROR_REPLY = (
    "This conversation isn't fully configured for Enso — ask an admin to run `enso config check`."
)
UNCONFIGURED_CHANNEL_REPLY = (
    "I haven't been enabled in this channel yet. Ask an Enso admin to set me up."
)
UNCONFIGURED_DM_REPLY = "I haven't been enabled for your DMs yet. Ask an Enso admin for access."
AUDIT_FAILURE_REPLY = (
    "This is an audited conversation and the audit record could not be "
    "written, so the request was not run."
)


def _key_digest(kind: str, *parts: object) -> str:
    """Build an opaque, delimiter-safe state key for a routed conversation."""
    payload = json.dumps({"v": 2, "kind": kind, "parts": list(parts)}, sort_keys=True)
    return f"teams:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


class TeamsRouter:
    """Resolve exact Slack routes and bind their workspace plus access profile."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.teams: TeamsConfig = load_teams(runtime.config)
        self.account_ok = False
        self._reported_problems = False

    def set_authenticated_account(self, team_id: str) -> None:
        """Require the configured account to match the authenticated token."""
        self.account_ok = bool(team_id and team_id == self.teams.account_id)
        if self.account_ok:
            log.info("Slack routes active for account %s", team_id)
        else:
            log.error(
                "routes.slack.account_id=%r does not match the authenticated "
                "Slack team %r — routed dispatch is disabled",
                self.teams.account_id,
                team_id,
            )
        self._report_config_problems()

    def _report_config_problems(self) -> None:
        if self._reported_problems:
            return
        self._reported_problems = True
        for error in self.teams.errors:
            log.error("Teams config error (dispatch disabled): %s", error)
        for name, problems in self.teams.workspace_errors.items():
            for problem in problems:
                log.error("Workspace %s: %s", name, problem)
        for name, problems in self.teams.access_errors.items():
            for problem in problems:
                log.error("Access profile %s: %s", name, problem)
        for route_id, problems in self.teams.route_errors.items():
            for problem in problems:
                log.error("Route %s (disabled): %s", route_id, problem)
        checked: set[tuple[str, str]] = set()
        routes = (*self.teams.dm_routes.values(), *self.teams.channel_routes.values())
        for route in routes:
            pair = (route.workspace, route.access)
            if pair in checked or not self.teams.route_usable(route):
                continue
            checked.add(pair)
            workspace = self.teams.workspaces[route.workspace]
            access = self.teams.access_profiles[route.access]
            for provider in access.providers:
                check = policy.check_provider(workspace, access, provider)
                for problem in check.problems:
                    log.error(
                        "Access profile %s on workspace %s cannot launch %s: %s",
                        access.name,
                        workspace.name,
                        provider,
                        problem,
                    )

    def startup_reconcile(self) -> None:
        """Close crash-orphaned audit records and apply startup retention."""
        for claim in ledger.abandon_pending():
            if claim.get("audit_turn_id"):
                audit.close_abandoned(claim["audit_turn_id"])
        audit.close_all_pending()
        ledger.prune()
        audit.prune(self.teams.audit_max_age_days)

    def surface_origin_authorized(self, origin: SurfaceDraftOrigin) -> bool:
        """Revalidate a stored surface draft against the current exact route."""
        if not self.account_ok or origin.account_id != self.teams.account_id:
            return False
        decision = resolve(
            self.teams,
            user_id=origin.user_id,
            channel_id=(None if origin.route_kind == "dm" else origin.channel_id),
        )
        route = decision.route
        return bool(
            decision.status == "authorized"
            and route is not None
            and route.route_id == origin.route_id
            and route.kind == origin.route_kind
            and route.workspace == origin.workspace_id
            and route.access == origin.access_profile
            and route.audit == origin.route_audit
        )

    async def handle_event(
        self,
        transport: SlackTransport,
        client: Any,
        event: dict,
        *,
        is_mention: bool,
    ) -> None:
        """Run one event through deduplication, exact routing, and dispatch."""
        user = event.get("user", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        if not user or not channel or not ts or not self.account_ok:
            return

        is_dm = event.get("channel_type") == "im" if not is_mention else channel.startswith("D")
        decision = resolve(
            self.teams,
            user_id=user,
            channel_id=None if is_dm else channel,
        )

        account = self.teams.account_id
        delivery = ledger.delivery_id(account, channel, ts)
        try:
            claimed = await asyncio.to_thread(ledger.claim, account, delivery)
        except Exception:
            log.exception("Slack delivery ledger claim failed; refusing event")
            return
        if not claimed:
            log.info("Duplicate Slack delivery acknowledged (%s…)", delivery[:12])
            return

        reply_thread = thread_ts or (ts if is_mention and not is_dm else None)
        if decision.status == "unconfigured":
            if self.teams.dispatchable:
                await self._finish_unconfigured(
                    transport,
                    client,
                    account,
                    delivery,
                    channel,
                    reply_thread,
                    user,
                    is_dm=is_dm,
                )
            else:
                await self._complete_ledger(account, delivery, None)
            return

        text = transport.flatten_mentions(event.get("text", ""), strip_addressing=True).strip()
        thread_key = thread_ts or (ts if not is_dm else None)
        conv_label = f"{channel}:{thread_key}" if thread_key else channel
        location_route = decision.route
        turn_fields = {
            "account_id": account,
            "delivery_id": delivery,
            "route_id": (location_route.route_id if location_route else "slack.unrouted"),
            "channel_id": channel,
            "thread_id": thread_ts,
            "source_message_id": ts,
            "conversation_id": conv_label,
            "user_id": user,
            "user_name": await asyncio.to_thread(transport.lookup_user_name, user),
            "request_text": text,
        }

        if decision.status == "error":
            await self._finish_config_error(
                transport,
                client,
                location_route,
                turn_fields,
                account,
                delivery,
                channel,
                reply_thread,
                user,
            )
            return

        await self._dispatch_authorized(
            transport,
            client,
            event,
            decision,
            turn_fields=turn_fields,
            account=account,
            delivery=delivery,
            channel=channel,
            ts=ts,
            thread_ts=thread_ts,
            thread_key=thread_key,
            conv_label=conv_label,
            reply_thread=reply_thread,
            user=user,
            text=text,
            is_dm=is_dm,
            is_mention=is_mention,
        )

    async def _finish_unconfigured(
        self,
        transport: SlackTransport,
        client: Any,
        account: str,
        delivery: str,
        channel: str,
        reply_thread: str | None,
        user: str,
        *,
        is_dm: bool,
    ) -> None:
        """Reply deterministically to explicit contact at an unrouted location."""
        ctx = transport.make_context(client, channel, reply_thread, user_id=user)
        reply = UNCONFIGURED_DM_REPLY if is_dm else UNCONFIGURED_CHANNEL_REPLY
        try:
            await ctx.reply(reply)
        except Exception:
            log.exception("Failed to deliver unconfigured-location reply")
        await self._complete_ledger(account, delivery, None)

    async def _finish_config_error(
        self,
        transport: SlackTransport,
        client: Any,
        route: Route | None,
        turn_fields: dict,
        account: str,
        delivery: str,
        channel: str,
        reply_thread: str | None,
        user: str,
    ) -> None:
        """Reply to an authorized location whose binding is unusable."""
        turn_id = None
        if route is not None and route.audit:
            try:
                fields = dict(turn_fields)
                fields.setdefault("workspace_id", route.workspace)
                turn_id = await asyncio.to_thread(
                    audit.create_turn,
                    decision="unconfigured",
                    response_text=CONFIG_ERROR_REPLY,
                    **fields,
                )
            except Exception:
                log.exception("Failed to record unconfigured turn")
        ctx = transport.make_context(client, channel, reply_thread, user_id=user)
        delivered = True
        try:
            await ctx.reply(CONFIG_ERROR_REPLY)
        except Exception:
            delivered = False
            log.exception("Failed to deliver configuration error")
        if turn_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(audit.record_delivery, turn_id, ok=delivered)
        await self._complete_ledger(account, delivery, turn_id)

    async def _dispatch_authorized(
        self,
        transport: SlackTransport,
        client: Any,
        event: dict,
        decision: Decision,
        *,
        turn_fields: dict,
        account: str,
        delivery: str,
        channel: str,
        ts: str,
        thread_ts: str | None,
        thread_key: str | None,
        conv_label: str,
        reply_thread: str | None,
        user: str,
        text: str,
        is_dm: bool,
        is_mention: bool,
    ) -> None:
        route = decision.route
        assert route is not None
        workspace = self.teams.workspaces[route.workspace]
        access = self.teams.access_profiles[route.access]
        chat_key = _key_digest(
            "conversation",
            account,
            channel,
            thread_key,
            workspace.name,
            access.name,
        )

        provider = self.runtime.active_provider_by_chat.get(chat_key)
        if provider not in access.providers:
            provider = access.default_provider
        if provider is None:
            await self._finish_config_error(
                transport,
                client,
                route,
                turn_fields,
                account,
                delivery,
                channel,
                reply_thread,
                user,
            )
            return
        self.runtime.active_provider_by_chat[chat_key] = provider
        self.runtime.touch_session(chat_key)

        command_parts = text[1:].split(None, 1) if text.startswith("!") else []
        command_name = command_parts[0].lower() if command_parts else None
        model = self.runtime.get_active_model(chat_key, provider)
        effort = self.runtime.get_active_effort(chat_key, provider, model)
        turn_fields.update(
            workspace_id=workspace.name,
            provider=None if command_name else provider,
            model=None if command_name else model,
        )

        if command_name is not None and not access.allows_command(command_name):
            await self._finish_denied_command(
                transport,
                client,
                route,
                turn_fields,
                account,
                delivery,
                channel,
                reply_thread,
                user,
                command_name,
            )
            return

        if command_name is None and not policy.check_provider(workspace, access, provider).ok:
            await self._finish_config_error(
                transport,
                client,
                route,
                turn_fields,
                account,
                delivery,
                channel,
                reply_thread,
                user,
            )
            return

        turn_id = None
        if route.audit:
            try:
                turn_id = await asyncio.to_thread(
                    audit.create_turn,
                    decision="accepted",
                    kind="command" if command_name else "provider",
                    **turn_fields,
                )
                await asyncio.to_thread(ledger.link_audit_turn, account, delivery, turn_id)
            except Exception:
                log.exception("Audit write failed for %s", route.route_id)
                if self.teams.audit_on_failure == "block":
                    ctx = transport.make_context(client, channel, reply_thread, user_id=user)
                    with contextlib.suppress(Exception):
                        await ctx.reply(AUDIT_FAILURE_REPLY)
                    await self._complete_ledger(account, delivery, None)
                    return

        ctx = transport.make_context(
            client,
            channel,
            reply_thread,
            user_id=user,
            audit_turn_id=turn_id,
            surface_origin=SurfaceDraftOrigin(
                account_id=account,
                route_id=route.route_id,
                route_kind=route.kind,
                workspace_id=workspace.name,
                access_profile=access.name,
                route_audit=route.audit,
                user_id=user,
                channel_id=channel,
                thread_ts=reply_thread,
                conversation_type=(
                    "im" if is_dm else str(event.get("channel_type") or "channel")
                ),
                audit_turn_id=turn_id,
            ),
            conversation_type=(
                "im" if is_dm else str(event.get("channel_type") or "channel")
            ),
        )
        execution = ExecutionContext(
            chat_key=chat_key,
            path=workspace.path,
            workspace_id=workspace.name,
            concurrency=workspace.concurrency,
            workspace=workspace,
            access=access,
            model=model,
            effort=effort,
            on_launch=self._make_launch_recorder(
                turn_id,
                provider,
                model,
            ),
            on_complete=self._make_completer(account, delivery, turn_id),
        )

        if command_name is not None:
            await self._run_command(
                transport,
                ctx,
                text,
                chat_key,
                workspace,
                access,
                account,
                delivery,
                turn_id,
                execution,
            )
            return

        try:
            prompt = await self._build_prompt(
                transport,
                client,
                event,
                workspace,
                channel=channel,
                ts=ts,
                thread_ts=thread_ts,
                text=text,
                is_mention=is_mention,
                is_dm=is_dm,
            )
        except Exception:
            log.exception("Could not build Slack prompt for %s", route.route_id)
            with contextlib.suppress(Exception):
                await ctx.reply("I couldn't prepare that request. Please try again.")
            await asyncio.to_thread(
                self._make_completer(account, delivery, turn_id),
                "error",
                "prompt_build_failed",
            )
            return

        if not prompt:
            await asyncio.to_thread(
                self._make_completer(account, delivery, turn_id),
                "ignored",
                "empty_request",
            )
            return

        log.info(
            "Teams dispatch: route=%s workspace=%s access=%s provider=%s len=%d",
            route.route_id,
            workspace.name,
            access.name,
            provider,
            len(prompt),
        )
        preview = text[:50].replace("\n", " ")
        await self.runtime.dispatch(
            conv_label,
            prompt,
            ctx,
            preview=preview,
            context=execution,
        )

    async def _finish_denied_command(
        self,
        transport: SlackTransport,
        client: Any,
        route: Route,
        turn_fields: dict,
        account: str,
        delivery: str,
        channel: str,
        reply_thread: str | None,
        user: str,
        command_name: str,
    ) -> None:
        reply = f"!{command_name} is not available in this conversation."
        turn_id = None
        if route.audit:
            with contextlib.suppress(Exception):
                turn_id = await asyncio.to_thread(
                    audit.create_turn,
                    decision="denied",
                    kind="command",
                    response_text=reply,
                    **turn_fields,
                )
        ctx = transport.make_context(client, channel, reply_thread, user_id=user)
        delivered = True
        try:
            await ctx.reply(reply)
        except Exception:
            delivered = False
        if turn_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(audit.record_delivery, turn_id, ok=delivered)
        await self._complete_ledger(account, delivery, turn_id)

    async def _run_command(
        self,
        transport: SlackTransport,
        ctx: SlackContext,
        text: str,
        chat_key: str,
        workspace: Workspace,
        access: AccessProfile,
        account: str,
        delivery: str,
        turn_id: str | None,
        command_context: ExecutionContext,
    ) -> None:
        outcome, reason = "error", "exception"
        try:
            response = await transport._handle_command(
                text,
                chat_key,
                ctx=ctx,
                workspace=workspace,
                access=access,
                allowed_providers=self._usable_providers(workspace, access),
                context=command_context,
            )
            if response:
                await ctx.reply(response)
            outcome, reason = "completed", None
        finally:
            if turn_id is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        audit.complete_turn,
                        turn_id,
                        outcome,
                        terminal_reason=reason,
                    )
            await self._complete_ledger(account, delivery, turn_id)

    @staticmethod
    def _usable_providers(workspace: Workspace, access: AccessProfile) -> list[str]:
        """Return providers both allowed by the profile and launchable now."""
        return [
            name for name in access.providers if policy.check_provider(workspace, access, name).ok
        ]

    async def _build_prompt(
        self,
        transport: SlackTransport,
        client: Any,
        event: dict,
        workspace: Workspace,
        *,
        channel: str,
        ts: str,
        thread_ts: str | None,
        text: str,
        is_mention: bool,
        is_dm: bool,
    ) -> str:
        """Build provider input from route context, attachments, and text."""
        from .slack import _attachment_files, _attachments_prompt, _file_prompt

        context_text = ""
        if thread_ts:
            context_text = await transport._fetch_thread_context(
                client,
                channel,
                thread_ts,
                author_filter=None,
                untrusted=True,
            )
        elif is_mention and not is_dm:
            context_text = await transport._fetch_channel_context(
                client,
                channel,
                ts,
                author_filter=None,
                untrusted=True,
            )

        attachments = event.get("attachments") or []
        shared_prompt = transport.flatten_mentions(_attachments_prompt(attachments))
        files = (event.get("files") or []) + _attachment_files(attachments)
        downloaded: list[str] = []
        if files:
            uploads_dir = transport.turn_uploads_dir(workspace.path, uuid.uuid4().hex[:8])
            downloaded = await transport._download_files(files, client, uploads_dir=uploads_dir)
        file_prompt = _file_prompt(downloaded, files)
        return "\n\n".join(
            part for part in (context_text, shared_prompt, file_prompt, text) if part
        )

    @staticmethod
    def _make_launch_recorder(
        turn_id: str | None,
        provider: str,
        model: str,
    ):
        if turn_id is None:
            return None

        def record(launch: Launch) -> None:
            audit.record_launch(
                turn_id,
                provider=provider,
                model=model,
                policy_revision=launch.policy_revision,
            )

        return record

    @staticmethod
    def _make_completer(
        account: str,
        delivery: str,
        turn_id: str | None,
    ):
        """Return idempotent terminal bookkeeping for one claimed event."""

        def on_complete(
            outcome: str = "completed",
            terminal_reason: str | None = None,
        ) -> None:
            if turn_id is not None:
                try:
                    audit.complete_turn(
                        turn_id,
                        outcome,
                        terminal_reason=terminal_reason,
                    )
                except Exception:
                    log.exception("Failed to complete audit turn %s", turn_id)
            try:
                ledger.complete(account, delivery, audit_turn_id=turn_id)
            except Exception:
                log.exception("Failed to complete ledger claim")

        return on_complete

    @staticmethod
    async def _complete_ledger(
        account: str,
        delivery: str,
        turn_id: str | None,
    ) -> None:
        try:
            await asyncio.to_thread(
                ledger.complete,
                account,
                delivery,
                audit_turn_id=turn_id,
            )
        except Exception:
            log.exception("Failed to complete ledger claim")
