"""Teams channel notification helpers for Kintone registration completion."""
from __future__ import annotations

import logging
import os
from urllib.parse import quote

import requests

_log = logging.getLogger("tks_to_kintone_app")

KINTONE_TARGET_PROD = "production"
KINTONE_TARGET_TEST = "test"
KINTONE_APP_URL_PROD = "https://manekiya.cybozu.com/k/211/"
KINTONE_APP_URL_TEST = "https://manekiya.cybozu.com/k/255/"
KINTONE_ORDER_FIELD_CODE = "f8257622"
KINTONE_SORT_FIELD_CODE = "f8256572"
KINTONE_VIEW_ID = "20"
TEAMS_WEBHOOK_URL_TEST_ENV = "TKS_TEAMS_WEBHOOK_URL_TEST_DEFAULT"
TEAMS_WEBHOOK_URL_PROD_ENV = "TKS_TEAMS_WEBHOOK_URL_PROD_DEFAULT"

# 初回起動時のテスト用Webhook URL（初期値）。
# 利用者が設定画面で値を変更したらそちらが優先される。
DEFAULT_TEAMS_TEST_WEBHOOK_URL = (
    "https://default9981f198f4cc4e779fd09bebd2ae22.71.environment.api.powerplatform.com:443"
    "/powerautomate/automations/direct/workflows/498e28a230a4429fbb7549d65668d430"
    "/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0"
    "&sig=adrb6WXegnIpiuM8fin0y44ETH19h3pzTyWF-Lpme_k"
)
# 初回起動時の本番用Webhook URL（初期値）。実運用名は「東大阪」。
DEFAULT_TEAMS_PROD_WEBHOOK_URL = (
    "https://default9981f198f4cc4e779fd09bebd2ae22.71.environment.api.powerplatform.com:443"
    "/powerautomate/automations/direct/workflows/935f20267d734174b36d6c6ab6c953c9"
    "/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0"
    "&sig=LBzsv2d-nIBEc6imAI3RhHcbN4-DlUcFjIKAyZ80c2U"
)


class TeamsNotifyError(Exception):
    """Teams notification failed."""


def default_teams_webhook_url_test() -> str:
    # 環境変数が優先。未設定なら同梱の初期値を使う。
    return str(os.environ.get(TEAMS_WEBHOOK_URL_TEST_ENV) or DEFAULT_TEAMS_TEST_WEBHOOK_URL).strip()


def default_teams_webhook_url_prod() -> str:
    # 環境変数が優先。未設定なら同梱の初期値（東大阪）を使う。
    return str(os.environ.get(TEAMS_WEBHOOK_URL_PROD_ENV) or DEFAULT_TEAMS_PROD_WEBHOOK_URL).strip()


def kintone_app_url_for_target(target: str) -> str:
    if target == KINTONE_TARGET_TEST:
        return KINTONE_APP_URL_TEST
    return KINTONE_APP_URL_PROD


def build_kintone_order_url(order_no: str, target: str = KINTONE_TARGET_PROD) -> str:
    """Build a Kintone app URL filtered by the exact order number."""
    value = str(order_no).strip()
    query = f'{KINTONE_ORDER_FIELD_CODE} = "{value}"'
    encoded_query = quote(query, safe="")
    return (
        f"{kintone_app_url_for_target(target)}"
        f"?view={KINTONE_VIEW_ID}"
        f"&q={encoded_query}"
        f"#sort_0={KINTONE_SORT_FIELD_CODE}&order_0=desc&size=20"
    )


def build_teams_order_links_payload(items: list[dict], target: str = KINTONE_TARGET_PROD) -> dict:
    """Build an Adaptive Card payload containing only linked order numbers."""
    lines: list[str] = []
    for item in items:
        value = str(item.get("order_no") or "").strip()
        if not value:
            continue
        label = str(item.get("label") or "").strip()
        text = f"{value}（{label}）" if label else value
        url = build_kintone_order_url(value, target=target)
        lines.append(f"[{text}]({url})")

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "\n".join(lines),
                            "wrap": True,
                        }
                    ],
                },
            }
        ],
    }


def post_teams_webhook(webhook_url: str, payload: dict, timeout: int = 10) -> None:
    """Post a payload to Teams without exposing the webhook URL in logs/errors."""
    if not webhook_url:
        raise TeamsNotifyError("Teams Webhook URLが未設定です。")

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception as exc:
        _log.warning("Teams通知に失敗しました")
        raise TeamsNotifyError("Teams通知に失敗しました。") from exc
