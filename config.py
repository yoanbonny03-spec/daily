"""
Configuration loader.

All sensitive values are read from environment variables or a .env file.
See .env.example for the full list of required variables.
"""

import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Try to load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
        logger.debug("Loaded .env from %s", _env_path)
except ImportError:
    pass


def _require(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Check your .env file or environment."
        )
    return value


def _optional(key: str, default=None):
    return os.environ.get(key, default)


def _json_list(key: str, default: list) -> list:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    # Fallback: comma-separated
    return [x.strip() for x in raw.split(",") if x.strip()]


def load_config() -> dict:
    return {
        # ---- AmoCRM ----
        # Your AmoCRM subdomain (e.g. "mycompany" for mycompany.amocrm.ru)
        "AMO_DOMAIN": _require("AMO_DOMAIN"),

        # Long-lived access token from AmoCRM OAuth
        "AMO_ACCESS_TOKEN": _require("AMO_ACCESS_TOKEN"),

        # Custom field IDs — find these via GET /api/v4/leads/custom_fields
        "AMO_CITY_FIELD_ID": _require("AMO_CITY_FIELD_ID"),
        "AMO_DEPARTMENT_FIELD_ID": _require("AMO_DEPARTMENT_FIELD_ID"),
        "AMO_END_DATE_FIELD_ID": _require("AMO_END_DATE_FIELD_ID"),

        # Pipeline names for new sales (contracts)
        # JSON array or comma-separated list
        "AMO_SALES_PIPELINE_NAMES": _json_list(
            "AMO_SALES_PIPELINE_NAMES",
            default=["Оффлайн"],
        ),

        # Pipeline IDs (fallback if names don't match)
        "AMO_SALES_PIPELINE_IDS": _json_list("AMO_SALES_PIPELINE_IDS", default=[]),

        # Pipeline names for expiring subscriptions
        "AMO_EXPIRES_PIPELINE_NAMES": _json_list(
            "AMO_EXPIRES_PIPELINE_NAMES",
            default=[
                "Ежемесячники (1 месяц)",
                "Абонементы (3-6 месяцев)",
                "До конца учебного года (7-12 месяцев)",
            ],
        ),

        # Pipeline IDs for expires (fallback)
        "AMO_EXPIRES_PIPELINE_IDS": _json_list("AMO_EXPIRES_PIPELINE_IDS", default=[]),

        # ID поля "Дата заключения договора" — используется для фильтрации новых продаж
        "AMO_CONTRACT_DATE_FIELD_ID": _require("AMO_CONTRACT_DATE_FIELD_ID"),

        # Названия этапов сделки, которые считаются "договор подписан"
        # Скрипт сам найдёт их ID по названию внутри воронки
        "AMO_WON_STATUS_NAMES": _json_list("AMO_WON_STATUS_NAMES", default=[
            "ПРЕДОПЛАТА получена №1",
            "СЧЕТ НА ОПЛАТУ ДЛЯ ПРЕДОПЛАТЫ №2",
            "ПРЕДОПЛАТА ПОЛУЧЕНА №2",
            "Счет на оплату для полной оплаты",
            "ПОЛНАЯ ОПЛАТА ПОЛУЧЕНА",
            "Реализован в HH",
            "Успешно реализовано",
        ]),

        # Filter values
        "CITY_VALUE": _optional("CITY_VALUE", "Астана"),
        "DEPARTMENT_VALUE": _optional("DEPARTMENT_VALUE", "Оффлайн"),

        # ---- Google Sheets ----
        # Either GOOGLE_SERVICE_ACCOUNT_JSON (for Railway/cloud) OR
        # GOOGLE_SERVICE_ACCOUNT_FILE (path to local JSON file) must be set.
        "GOOGLE_SERVICE_ACCOUNT_FILE": _optional("GOOGLE_SERVICE_ACCOUNT_FILE"),

        # Spreadsheet ID for the payments/employee sheets
        # (the one with employee sheets and "План еженедельный")
        "PAYMENT_SPREADSHEET_ID": _require("PAYMENT_SPREADSHEET_ID"),

        # Spreadsheet ID for the daily report (where we write results)
        "REPORT_SPREADSHEET_ID": _require("REPORT_SPREADSHEET_ID"),

        # Name of the worksheet tab in the daily report spreadsheet
        "REPORT_SHEET_NAME": _optional("REPORT_SHEET_NAME", "Февраль заполнение"),

        # Column letters in the report sheet
        "REPORT_CITY_COL": _optional("REPORT_CITY_COL", "A"),
        "REPORT_DATE_COL": _optional("REPORT_DATE_COL", "B"),
    }
