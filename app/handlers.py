"""Telegram transport; repository and checker are injected by the dispatcher."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db import Repository
from app.monitor import CheckInProgress, MonitoringService
from app.security import UnsafeURL, validate_url

router = Router()
HELP = (
    "PULSEBOT monitors your websites and APIs. Automatic checks run every 60 seconds "
    "by default while PULSEBOT is running.\n"
    "Receive DOWN alerts when an endpoint fails and UP alerts when it recovers. "
    "The first check establishes a baseline; unchanged states send no alerts.\n\n"
    "/start — show this guide\n"
    "/add <https-url> — save a public HTTPS endpoint\n"
    "/list — show your endpoints\n"
    "/remove <id> — delete one of your endpoints\n"
    "/check <id> — check status and response latency\n"
    "/status — show the latest state of your endpoints\n"
    "/history <id> — show the last 10 checks (UTC)\n"
    "/help — show this guide\n\n"
    "Use a private chat. Only public HTTPS endpoints on port 443 are supported; "
    "private and local addresses are blocked. "
    "Do not put secrets in URL paths or query parameters. "
    "Checks measure response headers."
)


@router.message(Command("start", "help"))
async def help_command(message: Message):
    await message.answer(HELP)


@router.message(Command("add", "list", "remove", "check", "status", "history"))
async def endpoint_command(
    message: Message, command: CommandObject, repository: Repository, monitor: MonitoringService
):
    if message.chat.type != "private" or message.from_user is None:
        await message.answer("Please use a private chat with PULSEBOT to manage your endpoints.")
        return
    user_id = message.from_user.id
    action = command.command
    args = (command.args or "").strip()
    try:
        if action == "add":
            if not args:
                await message.answer("Usage: /add https://example.com/health")
                return
            url = validate_url(args)
            endpoint = await repository.add(user_id, str(url))
            await message.answer(f"Saved endpoint #{endpoint.id}. Use /check {endpoint.id}.")
        elif action in {"list", "status"}:
            if args:
                await message.answer(f"Usage: /{action}")
                return
            endpoints = await repository.list(user_id)
            if not endpoints:
                await message.answer("No endpoints yet. Add one with /add https://example.com.")
            # Each URL is at most 2048 characters; individual messages stay below Telegram's limit.
            for endpoint in endpoints:
                if action == "list":
                    await message.answer(f"#{endpoint.id} — {endpoint.url}")
                else:
                    history = await repository.history(user_id, endpoint.id, limit=1)
                    if history:
                        latest = history[0]
                        state = "UP" if latest.healthy else "DOWN"
                        await message.answer(
                            f"#{endpoint.id}: {state} — HTTP {latest.status or 'unavailable'}\n"
                            f"{latest.latency_ms:.0f} ms at {latest.completed_at.isoformat()} UTC"
                        )
                    else:
                        await message.answer(f"#{endpoint.id}: UNKNOWN — awaiting the first check.")
        else:
            if (
                len(args) > 19
                or not args.isascii()
                or not args.isdecimal()
                or not 0 < int(args) < 2**63
            ):
                await message.answer(f"Usage: /{action} <positive endpoint id>")
                return
            endpoint_id = int(args)
            if action == "remove":
                removed = await repository.remove(user_id, endpoint_id)
                await message.answer("Endpoint removed." if removed else "Endpoint not found.")
            else:
                endpoint = await repository.get(user_id, endpoint_id)
                if endpoint is None:
                    await message.answer("Endpoint not found.")
                    return
                if action == "history":
                    history = await repository.history(user_id, endpoint_id)
                    if not history:
                        await message.answer("No checks recorded yet.")
                    else:
                        lines = [f"History for #{endpoint_id} (latest first, UTC):"]
                        for row in history:
                            state = "UP" if row.healthy else "DOWN"
                            lines.append(
                                f"{row.completed_at.isoformat()} {state} "
                                f"HTTP {row.status or 'unavailable'} {row.latency_ms:.0f} ms"
                            )
                        await message.answer("\n".join(lines))
                    return
                result = await monitor.check(endpoint)
                if result.error:
                    await message.answer(f"Check failed: {result.error}")
                else:
                    label = "Healthy" if result.healthy else "Unhealthy"
                    await message.answer(
                        f"#{endpoint.id}: {label} — HTTP {result.status}\n"
                        f"Response latency: {result.latency_ms:.0f} ms"
                    )
    except CheckInProgress:
        await message.answer("A check is already running for this endpoint. Try /status shortly.")
    except UnsafeURL as exc:
        await message.answer(str(exc))
    except IntegrityError:
        await message.answer("You already monitor this endpoint.")
    except SQLAlchemyError:
        await message.answer("Storage is temporarily unavailable. Please try again later.")
