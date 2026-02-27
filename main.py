"""
Daily report automation entry point.

Runs every morning at 10:00 (configured via --schedule or cron).
Fills the previous day's data for Астана in the Google Sheets daily report.

Usage:
    # Run once for yesterday (default):
    python main.py

    # Run for a specific date:
    python main.py --date 25.02.2026

    # Run as a scheduler (blocks, fires at 10:00 every day):
    python main.py --schedule
"""

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

import schedule
import time

from config import load_config
from amo_client import AmoCRMClient
from sheets_client import SheetsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("daily_report")


def run_for_date(target_date: date, cfg: dict) -> None:
    logger.info("=== Starting daily report for %s ===", target_date.strftime("%d.%m.%Y"))

    # ---- AmoCRM ----
    amo = AmoCRMClient(
        domain=cfg["AMO_DOMAIN"],
        access_token=cfg["AMO_ACCESS_TOKEN"],
    )

    # Resolve pipeline IDs by name
    pipeline_map = amo.get_pipeline_map()
    logger.debug("Available pipelines: %s", list(pipeline_map.keys()))

    # Pipelines for new sales (contracts)
    sales_pipeline_names = cfg.get("AMO_SALES_PIPELINE_NAMES", [
        "Новые продажи",
    ])
    sales_pipeline_ids = [
        pipeline_map[n] for n in sales_pipeline_names if n in pipeline_map
    ]
    if not sales_pipeline_ids:
        # Fallback: use raw IDs from config if names not found
        sales_pipeline_ids = cfg.get("AMO_SALES_PIPELINE_IDS", [])

    # Pipelines for expires
    expires_pipeline_names = cfg.get("AMO_EXPIRES_PIPELINE_NAMES", [
        "Ежемесячники (1 месяц)",
        "Абонементы (3-6 месяцев)",
        "До конца учебного года (7-12 месяцев)",
    ])
    expires_pipeline_ids = [
        pipeline_map[n] for n in expires_pipeline_names if n in pipeline_map
    ]
    if not expires_pipeline_ids:
        expires_pipeline_ids = cfg.get("AMO_EXPIRES_PIPELINE_IDS", [])

    # Custom field IDs and enum IDs for city/dept filtering
    city_field_id = int(cfg["AMO_CITY_FIELD_ID"])
    dept_field_id = int(cfg["AMO_DEPARTMENT_FIELD_ID"])
    city_enum_id = int(cfg["AMO_CITY_ENUM_ID"])
    dept_enum_id = int(cfg["AMO_DEPT_ENUM_ID"])
    end_date_field_id = int(cfg["AMO_END_DATE_FIELD_ID"])
    contract_date_field_id = int(cfg["AMO_CONTRACT_DATE_FIELD_ID"])

    # Resolve won status IDs — only leads in these 7 stages count as real sales
    won_status_names = cfg.get("AMO_WON_STATUS_NAMES", [])
    won_status_ids = amo.get_won_status_ids(sales_pipeline_ids, won_status_names)
    logger.info("Won status IDs resolved: %s", won_status_ids)

    # 1. New sales purch 1d-3d (col R) + New sales purch total (col S)
    logger.info("--- Шаг 1: запрос новых продаж из AmoCRM ---")
    new_sales_1d3d, new_sales_total = amo.count_new_sales(
        target_date=target_date,
        pipeline_ids=sales_pipeline_ids,
        contract_date_field_id=contract_date_field_id,
        won_status_ids=won_status_ids,
        city_field_id=city_field_id,
        city_enum_id=city_enum_id,
        dept_field_id=dept_field_id,
        dept_enum_id=dept_enum_id,
    )
    logger.info("New sales 1-3d: %d, total: %d", new_sales_1d3d, new_sales_total)

    # 2. Expires (col T)
    logger.info("--- Шаг 2: запрос истекающих подписок из AmoCRM ---")
    expires = amo.count_expires(
        target_date=target_date,
        end_date_field_id=end_date_field_id,
        pipeline_ids=expires_pipeline_ids,
        city_field_id=city_field_id,
        city_enum_id=city_enum_id,
        dept_field_id=dept_field_id,
        dept_enum_id=dept_enum_id,
    )
    logger.info("Expires: %d", expires)

    # ---- Google Sheets ----
    sheets = SheetsClient(service_account_file=cfg["GOOGLE_SERVICE_ACCOUNT_FILE"])

    payment_spreadsheet_id = cfg["PAYMENT_SPREADSHEET_ID"]

    # 3. Upsales purch (col U)
    logger.info("--- Шаг 3: подсчёт допродаж из Google Sheets ---")
    upsales_purch = sheets.count_upsales_purchases(payment_spreadsheet_id, target_date)
    logger.info("Upsales purch: %d", upsales_purch)

    weekly_plan_sheet = cfg.get("WEEKLY_PLAN_SHEET_NAME", "План еженедельный")

    # 4. New sales revenue (col V)
    logger.info("--- Шаг 4: выручка новых продаж из Google Sheets ---")
    new_sales_revenue = sheets.get_new_sales_revenue(payment_spreadsheet_id, target_date, weekly_plan_sheet)
    logger.info("New sales revenue: %.2f", new_sales_revenue)

    # 5. Upsales revenue (col W)
    logger.info("--- Шаг 5: выручка допродаж из Google Sheets ---")
    upsales_revenue = sheets.get_upsales_revenue(payment_spreadsheet_id, target_date, weekly_plan_sheet)
    logger.info("Upsales revenue: %.2f", upsales_revenue)

    # ---- Write to daily report ----
    logger.info("--- Шаг 6: запись в отчётную таблицу ---")
    ok = sheets.write_daily_report(
        report_spreadsheet_id=cfg["REPORT_SPREADSHEET_ID"],
        report_sheet_name=cfg.get("REPORT_SHEET_NAME", "Февраль заполнение"),
        target_date=target_date,
        new_sales_1d3d=new_sales_1d3d,
        new_sales_total=new_sales_total,
        expires=expires,
        upsales_purch=upsales_purch,
        new_sales_revenue=new_sales_revenue,
        upsales_revenue=upsales_revenue,
        city_col=cfg.get("REPORT_CITY_COL", "A"),
        date_col=cfg.get("REPORT_DATE_COL", "B"),
        city_value=cfg.get("CITY_VALUE", "Астана"),
    )

    if ok:
        logger.info("=== Daily report for %s completed successfully ===", target_date.strftime("%d.%m.%Y"))
    else:
        logger.error("=== Daily report for %s FAILED (row not found) ===", target_date.strftime("%d.%m.%Y"))
        sys.exit(1)


def job(cfg: dict) -> None:
    """Scheduled job: fill report for yesterday."""
    yesterday = date.today() - timedelta(days=1)
    try:
        run_for_date(yesterday, cfg)
    except Exception:
        logger.exception("Unhandled error in scheduled job")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily report automation")
    parser.add_argument(
        "--date",
        help="Target date in dd.mm.yyyy format (default: yesterday)",
        default=None,
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run as a daily scheduler (fires at 10:00 every day)",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.schedule:
        logger.info("Scheduler mode: will run every day at 05:00 UTC (10:00 Astana)")
        schedule.every().day.at("05:00").do(job, cfg=cfg)
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        if args.date:
            try:
                target_date = datetime.strptime(args.date, "%d.%m.%Y").date()
            except ValueError:
                logger.error("Invalid date format. Use dd.mm.yyyy, e.g. 25.02.2026")
                sys.exit(1)
        else:
            target_date = date.today() - timedelta(days=1)

        run_for_date(target_date, cfg)


if __name__ == "__main__":
    main()
