"""Static Slack routing for shared workspaces and native access profiles."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True)
class TurnContext:
    """One claimed Slack delivery and everything routing derives from it.

    Built once per event, immediately after the ledger claim. ``text`` and
    ``turn_fields`` are filled in afterwards because an unrouted location must
    reach its fixed reply without flattening mentions, resolving a name, or
    assembling an audit record.
    """

    transport: SlackTransport
    client: Any
    event: dict
    decision: Decision
    account: str
    delivery: str
    channel: str
    ts: str
    thread_ts: str | None
    thread_key: str | None
    conv_label: str
    reply_thread: str | None
    user: str
    is_dm: bool
    is_mention: bool
    text: str = ""
    turn_fields: dict = field(default_factory=dict)

    @property
    def addressed(self) -> bool:
        """Whether the message contacted Enso explicitly (a mention, or any DM).

        Explicit contact may receive fixed error replies; unaddressed traffic
        admitted by relaxed triggers must fail silently instead of spamming a
        broken responsive channel on every message.
        """
        return self.is_mention or self.is_dm


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

        # Unaddressed channel traffic engages only through a routed channel's
        # relaxed response triggers, and its drops happen before the ledger
        # claim so a busy fully-ignored channel writes nothing.
        if (
            not is_mention
            and not is_dm
            and not self._passes_response_triggers(decision, channel, thread_ts)
        ):
            return

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

        thread_key = thread_ts or (ts if not is_dm else None)
        turn = TurnContext(
            transport=transport,
            client=client,
            event=event,
            decision=decision,
            account=account,
            delivery=delivery,
            channel=channel,
            ts=ts,
            thread_ts=thread_ts,
            thread_key=thread_key,
            conv_label=f"{channel}:{thread_key}" if thread_key else channel,
            # A reply to a channel message always lands in that message's thread.
            reply_thread=thread_ts or (None if is_dm else ts),
            user=user,
            is_dm=is_dm,
            is_mention=is_mention,
        )

        if decision.status == "unconfigured":
            if self.teams.dispatchable:
                await self._finish_fixed_reply(
                    turn,
                    UNCONFIGURED_DM_REPLY if is_dm else UNCONFIGURED_CHANNEL_REPLY,
                )
            else:
                await self._complete_ledger(account, delivery, None)
            return

        text = transport.flatten_mentions(event.get("text", ""), strip_addressing=True).strip()
        location_route = decision.route
        turn = replace(
            turn,
            text=text,
            turn_fields={
                "account_id": account,
                "delivery_id": delivery,
                "route_id": (location_route.route_id if location_route else "slack.unrouted"),
                "channel_id": channel,
                "thread_id": thread_ts,
                "source_message_id": ts,
                "conversation_id": turn.conv_label,
                "user_id": user,
                "user_name": await asyncio.to_thread(transport.lookup_user_name, user),
                "request_text": text,
            },
        )

        if decision.status == "error":
            await self._finish_fixed_reply(turn, CONFIG_ERROR_REPLY)
            return

        await self._dispatch_authorized(turn)

    def _passes_response_triggers(
        self,
        decision: Decision,
        channel: str,
        thread_ts: str | None,
    ) -> bool:
        """Whether an unaddressed channel message engages its route.

        Only an authorized route's settings can admit one: unrouted and
        misconfigured channels stay silent for unaddressed traffic (explicit
        contact still receives their fixed replies through the normal flow).
        """
        route = decision.route
        if decision.status != "authorized" or route is None:
            return False
        if thread_ts is None:
            return not route.mention_required
        if route.thread_mention_required:
            return False
        return self._thread_participating(route, channel, thread_ts)

    def _thread_participating(self, route: Route, channel: str, thread_ts: str) -> bool:
        """Whether a prior authorized dispatch joined this thread.

        The per-thread conversation session doubles as the participation
        marker: it is recorded on every dispatch, persists with session
        state across restarts, and lapses with session retention pruning.
        """
        chat_key = _key_digest(
            "conversation",
            self.teams.account_id,
            channel,
            thread_ts,
            route.workspace,
            route.access,
        )
        return chat_key in self.runtime.active_provider_by_chat

    async def _finish_fixed_reply(
        self,
        turn: TurnContext,
        reply: str,
        *,
        audit_decision: str = "unconfigured",
        kind: str | None = None,
        notify: bool = True,
    ) -> None:
        """Record, deliver, and close one fixed transport reply.

        Covers every terminal refusal: an unrouted location (no route, so no
        audit record), an authorized location whose binding is unusable, and a
        command the access profile does not allow. An audited route records the
        refusal before it is sent and records whether it landed.

        ``notify=False`` (unaddressed traffic admitted by relaxed response
        triggers) keeps the audit record and ledger bookkeeping but stays
        silent: only explicit contact may surface the fixed error reply,
        otherwise a broken responsive channel would be spammed on every
        message.
        """
        route = turn.decision.route
        turn_id = None
        if route is not None and route.audit:
            try:
                fields = dict(turn.turn_fields)
                fields.setdefault("workspace_id", route.workspace)
                turn_id = await asyncio.to_thread(
                    audit.create_turn,
                    decision=audit_decision,
                    kind=kind,
                    response_text=reply if notify else None,
                    **fields,
                )
            except Exception:
                log.exception("Failed to record %s turn", audit_decision)
        if not notify:
            log.warning(
                "Suppressed config-error reply for unaddressed message in %s",
                turn.channel,
            )
            await self._complete_ledger(turn.account, turn.delivery, turn_id)
            return
        ctx = turn.transport.make_context(
            turn.client,
            turn.channel,
            turn.reply_thread,
            user_id=turn.user,
        )
        delivered = True
        try:
            await ctx.reply(reply)
        except Exception:
            delivered = False
            log.exception("Failed to deliver fixed %s reply", audit_decision)
        if turn_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(audit.record_delivery, turn_id, ok=delivered)
        await self._complete_ledger(turn.account, turn.delivery, turn_id)

    async def _dispatch_authorized(self, turn: TurnContext) -> None:
        route = turn.decision.route
        assert route is not None
        transport = turn.transport
        workspace = self.teams.workspaces[route.workspace]
        access = self.teams.access_profiles[route.access]
        chat_key = _key_digest(
            "conversation",
            turn.account,
            turn.channel,
            turn.thread_key,
            workspace.name,
            access.name,
        )

        provider = self.runtime.active_provider_by_chat.get(chat_key)
        if provider not in access.providers:
            provider = access.default_provider
        if provider is None:
            await self._finish_fixed_reply(turn, CONFIG_ERROR_REPLY, notify=turn.addressed)
            return
        self.runtime.active_provider_by_chat[chat_key] = provider
        self.runtime.touch_session(chat_key)

        # Commands require explicit addressing (a mention, or any DM); an
        # unaddressed "!text" in a responsive channel is ordinary prompt text.
        text = turn.text
        command_parts = text[1:].split(None, 1) if turn.addressed and text.startswith("!") else []
        command_name = command_parts[0].lower() if command_parts else None
        model = self.runtime.get_active_model(chat_key, provider)
        effort = self.runtime.get_active_effort(chat_key, provider, model)
        turn.turn_fields.update(
            workspace_id=workspace.name,
            provider=None if command_name else provider,
            model=None if command_name else model,
        )

        if command_name is not None and not access.allows_command(command_name):
            await self._finish_fixed_reply(
                turn,
                f"!{command_name} is not available in this conversation.",
                audit_decision="denied",
                kind="command",
            )
            return

        if command_name is None and not policy.check_provider(workspace, access, provider).ok:
            await self._finish_fixed_reply(turn, CONFIG_ERROR_REPLY, notify=turn.addressed)
            return

        turn_id = None
        if route.audit:
            try:
                turn_id = await asyncio.to_thread(
                    audit.create_turn,
                    decision="accepted",
                    kind="command" if command_name else "provider",
                    **turn.turn_fields,
                )
                await asyncio.to_thread(
                    ledger.link_audit_turn,
                    turn.account,
                    turn.delivery,
                    turn_id,
                )
            except Exception:
                log.exception("Audit write failed for %s", route.route_id)
                if self.teams.audit_on_failure == "block":
                    if turn.addressed:
                        ctx = transport.make_context(
                            turn.client,
                            turn.channel,
                            turn.reply_thread,
                            user_id=turn.user,
                        )
                        with contextlib.suppress(Exception):
                            await ctx.reply(AUDIT_FAILURE_REPLY)
                    await self._complete_ledger(turn.account, turn.delivery, None)
                    return

        conversation_type = (
            "im" if turn.is_dm else str(turn.event.get("channel_type") or "channel")
        )
        ctx = transport.make_context(
            turn.client,
            turn.channel,
            turn.reply_thread,
            user_id=turn.user,
            audit_turn_id=turn_id,
            surface_origin=SurfaceDraftOrigin(
                account_id=turn.account,
                route_id=route.route_id,
                route_kind=route.kind,
                workspace_id=workspace.name,
                access_profile=access.name,
                route_audit=route.audit,
                user_id=turn.user,
                channel_id=turn.channel,
                thread_ts=turn.reply_thread,
                conversation_type=conversation_type,
                audit_turn_id=turn_id,
            ),
            conversation_type=conversation_type,
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
            on_complete=self._make_completer(turn.account, turn.delivery, turn_id),
        )

        if command_name is not None:
            await self._run_command(turn, ctx, workspace, access, execution, turn_id)
            return

        try:
            prompt = await self._build_prompt(turn, workspace)
        except Exception:
            log.exception("Could not build Slack prompt for %s", route.route_id)
            with contextlib.suppress(Exception):
                await ctx.reply("I couldn't prepare that request. Please try again.")
            await asyncio.to_thread(
                self._make_completer(turn.account, turn.delivery, turn_id),
                "error",
                "prompt_build_failed",
            )
            return

        if not prompt:
            await asyncio.to_thread(
                self._make_completer(turn.account, turn.delivery, turn_id),
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
            turn.conv_label,
            prompt,
            ctx,
            preview=preview,
            context=execution,
        )

    async def _run_command(
        self,
        turn: TurnContext,
        ctx: SlackContext,
        workspace: Workspace,
        access: AccessProfile,
        command_context: ExecutionContext,
        turn_id: str | None,
    ) -> None:
        outcome, reason = "error", "exception"
        try:
            response = await turn.transport.handle_command(
                turn.text,
                command_context.chat_key,
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
            await self._complete_ledger(turn.account, turn.delivery, turn_id)

    @staticmethod
    def _usable_providers(workspace: Workspace, access: AccessProfile) -> list[str]:
        """Return providers both allowed by the profile and launchable now."""
        return [
            name for name in access.providers if policy.check_provider(workspace, access, name).ok
        ]

    async def _build_prompt(self, turn: TurnContext, workspace: Workspace) -> str:
        """Build provider input from route context, attachments, and text."""
        from .slack import _attachment_files, _attachments_prompt, _file_prompt

        transport = turn.transport
        context_text = ""
        if turn.thread_ts:
            context_text = await transport.fetch_thread_context(
                turn.client,
                turn.channel,
                turn.thread_ts,
                author_filter=None,
                untrusted=True,
            )
        elif not turn.is_dm:
            context_text = await transport.fetch_channel_context(
                turn.client,
                turn.channel,
                turn.ts,
                author_filter=None,
                untrusted=True,
            )

        attachments = turn.event.get("attachments") or []
        shared_prompt = transport.flatten_mentions(_attachments_prompt(attachments))
        files = (turn.event.get("files") or []) + _attachment_files(attachments)
        downloaded: list[str] = []
        if files:
            uploads_dir = transport.turn_uploads_dir(workspace.path, uuid.uuid4().hex[:8])
            downloaded = await transport.download_files(
                files,
                turn.client,
                uploads_dir=uploads_dir,
            )
        file_prompt = _file_prompt(downloaded, files)
        return "\n\n".join(
            part for part in (context_text, shared_prompt, file_prompt, turn.text) if part
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
