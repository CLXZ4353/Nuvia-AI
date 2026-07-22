"""Dedicated Telegram long-polling worker for production deployments."""

from src.gui import run_telegram_polling_worker


def main() -> None:
    run_telegram_polling_worker()


if __name__ == "__main__":
    main()
