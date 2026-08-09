"""Slack teams-mode router: dedup → resolve → authorize → audit → dispatch.

Owns the per-event pipeline from teams.md § Resolution. The transport hands
every Slack event here when ``routes.slack`` is configured; the router
claims the delivery in the ledger, resolves the sender against groups and
exact routes, binds the workspace/policy execution context, gates chat
commands, and records the audit turn when the route opts in.

Silence and errors stay distinct: unknown or disallowed senders learn
nothing — even when storage or diagnostics fail — while an authorized
sender with an unusable route gets a specific configuration error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from .. import audit, ledger, policy
from ..config import load_config
from ..core import ExecutionContext
from ..teams import (
    Decision,
    Route,
    TeamsConfig,
    Workspace,
    binding_revision,
    load_teams,
    resolve,
)

if TYPE_CHECKING:
    from ..core import Runtime
    from .slack import SlackContext, SlackTransport

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@\w+>\s*")

CONFIG_ERROR_REPLY = (
    "This conversation isn't fully configured for Enso — ask an admin to run "
    "`enso policy check`."
)
AUDIT_FAILURE_REPLY = (
    "This is an audited conversation and the audit record could not be "
    "written, so the request was not run."
)


def _key_digest(kind: str, *parts: object) -> str:
    """Versioned structured key digest — never a delimiter-joined string."""
    payload = json.dumps({"v": 1, "kind": kind, "parts": list(parts)}, sort_keys=True)
    return f"teams:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


class TeamsRouter:
    """Per-event Slack teams pipeline, owned by the Slack transport."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.teams: TeamsConfig = load_teams(runtime.config)  # type: ignore[assignment]
        assert self.teams is not None, "TeamsRouter requires routes.slack"
        self.account_ok = False
        self._reported_problems = False

    # -- startup --

    def set_authenticated_account(self, team_id: str) -> None:
        """Compare the token's team against config; mismatch disables dispatch."""
        if team_id and team_id == self.teams.account_id:
            self.account_ok = True
            log.info("Slack teams mode active for account %s", team_id)
        else:
            self.account_ok = False
            log.error(
                "routes.slack.account_id=%r does not match the authenticated "
                "Slack team %r — teams dispatch is disabled",
                self.teams.account_id, team_id,
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
        for route_id, problems in self.teams.route_errors.items():
            for problem in problems:
                log.error("Route %s (disabled): %s", route_id, problem)
        audited = [
            r.route_id
            for r in (*self.teams.dm_routes.values(), *self.teams.channel_routes.values())
            if r.audit
        ]
        if audited:
            log.info("Audited Slack routes: %s", ", ".join(sorted(audited)))

    def startup_reconcile(self) -> None:
        """Close crash-orphaned claims/turns and apply retention. Sync."""
        for claim in ledger.abandon_pending():
            if claim.get("audit_turn_id"):
                audit.close_abandoned(claim["audit_turn_id"])
        ledger.prune()
        audit.prune(self.teams.audit_max_age_days)

    # -- event pipeline --

    async def handle_event(
        self, transport: SlackTransport, client: Any, event: dict, *, is_mention: bool
    ) -> None:
        """Run one Slack event through the full teams pipeline."""
        teams = self.teams
        user = event.get("user", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        if not user or not channel or not ts:
            return
        if not self.account_ok:
            return  # mismatched account: silence for everyone, logged at start

        # Only `im` conversations are DMs; everything else is an exact
        # channel route. app_mention events carry no channel_type, so DM
        # mentions are recognized by Slack's D-prefixed conversation IDs.
        is_dm = (
            event.get("channel_type") == "im" if not is_mention
            else channel.startswith("D")
        )
        account = teams.account_id

        # Claim the delivery before any other work: both event types for one
        # message and every Slack retry share this ID, so a duplicate claim
        # acknowledges without executing. A ledger failure blocks execution.
        delivery = ledger.delivery_id(account, channel, ts)
        try:
            claimed = await asyncio.to_thread(ledger.claim, account, delivery)
        except Exception:
            log.exception("Slack delivery ledger claim failed; refusing event")
            return
        if not claimed:
            log.info("Duplicate Slack delivery acknowledged (%s…)", delivery[:12])
            return

        decision = resolve(teams, user_id=user, channel_id=None if is_dm else channel)
        text = _MENTION_RE.sub("", event.get("text", "")).strip()
        thread_key = thread_ts or (ts if not is_dm else None)
        conv_label = f"{channel}:{thread_key}" if thread_key else channel
        reply_thread = thread_ts or (ts if is_mention and not is_dm else None)
        # The route whose location this is, for auditing ignored triggers on
        # audited routes even when the sender itself resolved to nothing.
        location_route = (
            decision.route
            if decision.route is not None
            else (None if is_dm else teams.channel_routes.get(channel))
        )

        turn_fields = {
            "account_id": account,
            "delivery_id": delivery,
            "route_id": location_route.route_id if location_route else "slack.unrouted",
            "channel_id": channel,
            "thread_id": thread_ts,
            "source_message_id": ts,
            "conversation_id": conv_label,
            "user_id": user,
            "user_name": transport.lookup_user_name(user),
            "groups": decision.groups,
            "authorized_groups": decision.authorized_groups or None,
            "request_text": text,
        }

        if decision.status == "silent":
            await self._finish_silent(location_route, turn_fields, account, delivery)
            return

        if decision.status == "error":
            await self._finish_config_error(
                transport, client, location_route, turn_fields,
                account, delivery, channel, reply_thread, user,
            )
            return

        await self._dispatch_authorized(
            transport, client, event, decision,
            turn_fields=turn_fields,
            account=account, delivery=delivery, channel=channel, ts=ts,
            thread_ts=thread_ts, thread_key=thread_key, conv_label=conv_label,
            reply_thread=reply_thread, user=user, text=text,
            is_dm=is_dm, is_mention=is_mention,
        )

    async def _finish_silent(
        self, route: Route | None, turn_fields: dict, account: str, delivery: str
    ) -> None:
        """Silence — recorded when the matched location is audited."""
        turn_id = None
        if route is not None and route.audit:
            try:
                turn_id = await asyncio.to_thread(
                    audit.create_turn, decision="ignored", **turn_fields
                )
            except Exception:
                # The sender must stay silent even when the audit write fails.
                log.exception("Failed to record ignored trigger")
        await self._complete_ledger(account, delivery, turn_id)

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
        """Authorized sender, unusable route: explicit error, no spawn."""
        audited = route is not None and route.audit
        turn_id = None
        if audited:
            try:
                turn_id = await asyncio.to_thread(
                    audit.create_turn,
                    decision="unconfigured",
                    response_text=CONFIG_ERROR_REPLY,
                    workspace_id=route.workspace if route else None,
                    **turn_fields,
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
            try:
                await asyncio.to_thread(audit.record_delivery, turn_id, ok=delivered)
            except Exception:
                log.exception("Failed to record delivery state")
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
        teams = self.teams
        route = decision.route
        assert route is not None
        workspace = teams.workspaces[route.workspace]
        brev = binding_revision(teams, route)

        # Provider selection is scoped to conversation + workspace + binding
        # revision; a fresh binding starts at the workspace default.
        sel_key = _key_digest("sel", account, channel, thread_key, workspace.name, brev)
        provider = self.runtime.active_provider_by_chat.get(sel_key)
        if provider not in workspace.providers:
            provider = workspace.default_provider
        if provider is None:
            await self._finish_config_error(
                transport, client, route, turn_fields,
                account, delivery, channel, reply_thread, user,
            )
            return

        try:
            launch = await asyncio.to_thread(policy.prepare_launch, workspace, provider)
        except policy.PolicyError as exc:
            log.error("Policy launch refused for %s: %s", route.route_id, exc)
            await self._finish_config_error(
                transport, client, route, turn_fields,
                account, delivery, channel, reply_thread, user,
            )
            return

        chat_key = _key_digest(
            "exec", account, channel, thread_key, workspace.name, brev,
            provider, launch.policy_revision,
        )
        self.runtime.active_provider_by_chat[chat_key] = provider
        self.runtime.active_provider_by_chat[sel_key] = provider

        command_name = text[1:].split(None, 1)[0].lower() if text.startswith("!") else None
        turn_fields.update(
            workspace_id=workspace.name,
            binding_revision=brev,
            policy_revision=launch.policy_revision,
            provider=None if command_name else provider,
            model=None if command_name else self.runtime.get_active_model(
                chat_key, provider
            ),
        )

        if command_name is not None and not workspace.allows_command(command_name):
            await self._finish_denied_command(
                transport, client, route, turn_fields,
                account, delivery, channel, reply_thread, user, command_name,
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
                await asyncio.to_thread(
                    ledger.link_audit_turn, account, delivery, turn_id
                )
            except Exception:
                log.exception("Audit write failed for %s", route.route_id)
                if teams.audit_on_failure == "block":
                    ctx = transport.make_context(
                        client, channel, reply_thread, user_id=user
                    )
                    try:
                        await ctx.reply(AUDIT_FAILURE_REPLY)
                    except Exception:
                        log.exception("Failed to deliver audit-failure reply")
                    await self._complete_ledger(account, delivery, None)
                    return

        ctx = transport.make_context(
            client, channel, reply_thread, user_id=user, audit_turn_id=turn_id
        )

        if command_name is not None:
            command_context = ExecutionContext(
                chat_key=chat_key,
                path=workspace.path,
                workspace_id=workspace.name,
                launch=launch,
            )
            await self._run_command(
                transport, ctx, text, chat_key, sel_key, workspace,
                account, delivery, turn_id, command_context,
            )
            return

        execution = ExecutionContext(
            chat_key=chat_key,
            path=workspace.path,
            workspace_id=workspace.name,
            launch=launch,
            revalidate=self._make_revalidator(
                user=user, is_dm=is_dm, channel=channel, route=route,
                brev=brev, provider=provider,
                policy_revision=launch.policy_revision, turn_id=turn_id,
            ),
            on_complete=self._make_completer(account, delivery, turn_id),
        )

        prompt = await self._build_prompt(
            transport, client, event, route, workspace,
            channel=channel, ts=ts, thread_ts=thread_ts,
            text=text, is_mention=is_mention, is_dm=is_dm,
        )
        if not prompt:
            await self._complete_ledger(account, delivery, turn_id)
            return

        preview = text[:50].replace("\n", " ")
        log.info(
            "Teams dispatch: route=%s workspace=%s provider=%s len=%d",
            route.route_id, workspace.name, provider, len(prompt),
        )
        await self.runtime.dispatch(
            conv_label, prompt, ctx, preview=preview, context=execution
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
            try:
                turn_id = await asyncio.to_thread(
                    audit.create_turn,
                    decision="denied", kind="command", response_text=reply,
                    **turn_fields,
                )
            except Exception:
                log.exception("Failed to record denied command")
        ctx = transport.make_context(client, channel, reply_thread, user_id=user)
        delivered = True
        try:
            await ctx.reply(reply)
        except Exception:
            delivered = False
        if turn_id is not None:
            try:
                await asyncio.to_thread(audit.record_delivery, turn_id, ok=delivered)
            except Exception:
                log.exception("Failed to record delivery state")
        await self._complete_ledger(account, delivery, turn_id)

    async def _run_command(
        self,
        transport: SlackTransport,
        ctx: SlackContext,
        text: str,
        chat_key: str,
        sel_key: str,
        workspace: Workspace,
        account: str,
        delivery: str,
        turn_id: str | None,
        command_context: ExecutionContext,
    ) -> None:
        usable = self._usable_providers(workspace)
        try:
            response = await transport._handle_command(
                text, chat_key, ctx=ctx,
                workspace=workspace, allowed_providers=usable, sel_key=sel_key,
                context=command_context,
            )
            if response:
                await ctx.reply(response)
        finally:
            if turn_id is not None:
                try:
                    await asyncio.to_thread(audit.complete_turn, turn_id, "completed")
                except Exception:
                    log.exception("Failed to complete command audit turn")
            await self._complete_ledger(account, delivery, turn_id)

    def _usable_providers(self, workspace: Workspace) -> list[str]:
        """Providers `!use` may offer: allowlisted and policy-usable."""
        return [
            name
            for name in workspace.providers
            if policy.check_provider(workspace, name).ok
        ]

    async def _build_prompt(
        self,
        transport: SlackTransport,
        client: Any,
        event: dict,
        route: Route,
        workspace: Workspace,
        *,
        channel: str,
        ts: str,
        thread_ts: str | None,
        text: str,
        is_mention: bool,
        is_dm: bool,
    ) -> str:
        """Context, forwarded content, files, and the request — teams rules.

        Surrounding context is untrusted input: with ``context_from:
        "allowed"`` only messages authored by the route's allowed groups (and
        Enso itself) are injected, and every injected message carries its
        author and an untrusted-content marker.
        """
        from .slack import _attachment_files, _attachments_prompt, _file_prompt

        allowed_users: frozenset[str] | None = None
        if route.context_from == "allowed":
            allowed_users = frozenset().union(
                *(self.teams.groups.get(g, frozenset()) for g in route.allow)
            )

        context_text = ""
        if thread_ts:
            context_text = await transport._fetch_thread_context(
                client, channel, thread_ts,
                allowed_users=allowed_users, untrusted=True,
            )
        elif is_mention and not is_dm:
            context_text = await transport._fetch_channel_context(
                client, channel, ts,
                allowed_users=allowed_users, untrusted=True,
            )

        attachments = event.get("attachments") or []
        shared_prompt = _attachments_prompt(attachments)
        files = (event.get("files") or []) + _attachment_files(attachments)
        downloaded: list[str] = []
        if files:
            uploads_dir = transport.turn_uploads_dir(workspace.path, uuid.uuid4().hex[:8])
            downloaded = await transport._download_files(
                files, client, uploads_dir=uploads_dir
            )
        file_prompt = _file_prompt(downloaded, files)

        parts = [p for p in (context_text, shared_prompt, file_prompt, text) if p]
        return "\n\n".join(parts)

    # -- revalidation and completion closures --

    def _make_revalidator(
        self,
        *,
        user: str,
        is_dm: bool,
        channel: str,
        route: Route,
        brev: str,
        provider: str,
        policy_revision: str,
        turn_id: str | None,
    ):
        """Re-resolve against current config immediately before execution."""

        def revalidate() -> str | None:
            verdict = self._revalidate_now(
                user=user, is_dm=is_dm, channel=channel, route=route,
                brev=brev, provider=provider, policy_revision=policy_revision,
            )
            if verdict is not None and turn_id is not None:
                try:
                    audit.complete_turn(
                        turn_id,
                        "ignored" if verdict == "revoked" else "blocked",
                        terminal_reason=(
                            "access_revoked" if verdict == "revoked"
                            else "resolution_changed"
                        ),
                    )
                except Exception:
                    log.exception("Failed to record stale-turn refusal")
            return verdict

        return revalidate

    def _revalidate_now(
        self,
        *,
        user: str,
        is_dm: bool,
        channel: str,
        route: Route,
        brev: str,
        provider: str,
        policy_revision: str,
    ) -> str | None:
        current = load_teams(load_config())
        if current is None or not current.dispatchable:
            return "teams_config_invalid"
        decision = resolve(
            current, user_id=user, channel_id=None if is_dm else channel
        )
        if decision.status == "silent":
            return "revoked"
        if decision.status != "authorized" or decision.route is None:
            return "resolution_changed"
        if decision.route.route_id != route.route_id:
            return "resolution_changed"
        if binding_revision(current, decision.route) != brev:
            return "resolution_changed"
        workspace = current.workspaces.get(decision.route.workspace)
        if workspace is None or not workspace.allows_provider(provider):
            return "resolution_changed"
        check = policy.check_provider(workspace, provider)
        if not check.ok or check.policy_revision != policy_revision:
            return "resolution_changed"
        return None

    def _make_completer(self, account: str, delivery: str, turn_id: str | None):
        """Terminal bookkeeping for a dispatched turn. Idempotent, sync."""

        def on_complete() -> None:
            if turn_id is not None:
                try:
                    audit.complete_turn(turn_id, "completed")
                except Exception:
                    log.exception("Failed to complete audit turn %s", turn_id)
            try:
                ledger.complete(account, delivery, audit_turn_id=turn_id)
            except Exception:
                log.exception("Failed to complete ledger claim")

        return on_complete

    async def _complete_ledger(
        self, account: str, delivery: str, turn_id: str | None
    ) -> None:
        try:
            await asyncio.to_thread(
                ledger.complete, account, delivery, audit_turn_id=turn_id
            )
        except Exception:
            log.exception("Failed to complete ledger claim")
