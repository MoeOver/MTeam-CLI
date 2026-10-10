"""Send the account-status reminder now (all accounts, or one)."""

from __future__ import annotations

import argparse
import logging

from mteam_cli.automation.remind import run_remind_tick
from mteam_cli.cli._account import add_account_arg
from mteam_cli.core.config import Settings


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "remind",
        help="立即发送保号状态提醒（最后登录 / 删号截止 / VIP；默认所有账户，--account 限定单个）。",
    )
    add_account_arg(p)
    p.set_defaults(func=handle)


async def handle(
    args: argparse.Namespace, settings: Settings, logger: logging.Logger
) -> int:
    targets = [settings.resolve_account(args.account)] if args.account else settings.accounts
    for account in targets:
        await run_remind_tick(account, settings, logger)
    return 0
