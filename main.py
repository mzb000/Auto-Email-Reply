from __future__ import annotations

import argparse
import sys
from pathlib import Path

from auto_reply import run_auto_reply
from send_emails import load_recipients_from_file, send_bulk_emails


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Email automation toolkit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="Send a bulk email campaign")
    send_parser.add_argument(
        "recipients",
        type=Path,
        help="Path to a text/CSV file containing recipient email addresses (one per line)",
    )
    send_parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Optional override for the email template path",
    )
    send_parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Seconds to wait between messages (defaults to config SEND_DELAY)",
    )
    send_parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Override default batch size before pausing",
    )
    send_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the campaign without sending any emails",
    )

    subparsers.add_parser("auto-reply", help="Start the auto-reply daemon (respects AUTO_REPLY toggle)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    if args.command == "send":
        recipients = load_recipients_from_file(args.recipients)
        send_bulk_emails(
            recipients,
            template_path=args.template,
            delay=args.delay,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        return 0

    if args.command == "auto-reply":
        run_auto_reply()
        return 0

    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
