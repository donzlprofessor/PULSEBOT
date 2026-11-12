import asyncio

from aiogram import Bot, Dispatcher
from pydantic import ValidationError

from app.checker import HealthChecker
from app.config import Settings
from app.db import Repository
from app.handlers import router
from app.monitor import MonitoringService


async def main() -> None:
    try:
        settings = Settings()
    except ValidationError:
        raise SystemExit("Invalid configuration. Check your .env against .env.example.") from None
    # Avoid logging configuration, Telegram request URLs or exception tracebacks with secrets.
    try:
        bot = Bot(token=settings.bot_token.get_secret_value())
    except Exception:
        raise SystemExit("BOT_TOKEN is invalid. Update your .env file.") from None
    repository = Repository(settings.database_url)
    monitor = None
    try:
        monitor = MonitoringService(
            repository,
            HealthChecker(
                settings.http_timeout_seconds,
                settings.max_redirects,
                settings.max_concurrent_checks,
            ),
            bot,
            interval=settings.monitor_interval_seconds,
            concurrency=settings.max_concurrent_checks,
        )
        # Acquire the database lock before schema initialization or polling starts.
        try:
            await monitor.start()
        except RuntimeError:
            raise SystemExit("Another PULSEBOT process is already using this database.") from None
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        await dispatcher.start_polling(
            bot,
            repository=repository,
            monitor=monitor,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        if monitor is not None:
            await monitor.stop()
        await bot.session.close()
        await repository.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise SystemExit(
            "PULSEBOT stopped. Check configuration, database and connectivity."
        ) from None
