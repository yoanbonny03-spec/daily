"""
Google Sheets client for reading payment data and writing daily report.
"""

import logging
from datetime import date
from typing import Optional

import json
import os

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Column indices (0-based) in payment employee sheets
PAYMENT_TYPE_COL = 5   # Column F
PAYMENT_DATE_COL = 8   # Column I
PAYMENT_TYPE_VALUE = "Повторные продажи"

# Row indices (1-based) in "План еженедельный" sheet
DATE_FROM_ROW = 2
DATE_FROM_COL = "A"   # A2
DATE_TO_COL = "B"     # B2
NEW_SALES_REV_ROW_START = 47   # D47
NEW_SALES_REV_ROW_END = 48     # D48
UPSALES_REV_ROW_START = 49     # D49
UPSALES_REV_ROW_END = 50       # D50
REVENUE_COL = "D"

# Employee sheets to skip when iterating payment spreadsheet
NON_EMPLOYEE_SHEETS = {
    "План еженедельный",
    "Сводная",
    "Лист1",
    "Sheet1",
}


class SheetsClient:
    """Thin wrapper around gspread for our specific use-cases."""

    def __init__(self, service_account_file: str = None):
        # Railway / cloud: credentials passed as JSON string in env var
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if sa_json:
            info = json.loads(sa_json)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
        self.gc = gspread.authorize(creds)

    # ------------------------------------------------------------------
    # Payment spreadsheet helpers
    # ------------------------------------------------------------------

    def _open_spreadsheet(self, spreadsheet_id: str) -> gspread.Spreadsheet:
        return self.gc.open_by_key(spreadsheet_id)

    def count_upsales_purchases(self, spreadsheet_id: str, target_date: date) -> int:
        """
        Count rows across all employee sheets where:
          column F == "Повторные продажи"  AND  column I == target_date
        """
        ss = self._open_spreadsheet(spreadsheet_id)
        target_str = target_date.strftime("%d.%m.%Y")
        total = 0

        for ws in ss.worksheets():
            if ws.title in NON_EMPLOYEE_SHEETS:
                continue

            try:
                all_values = ws.get_all_values()
            except Exception as exc:
                logger.warning("Could not read sheet %s: %s", ws.title, exc)
                continue

            # Skip header row (index 0)
            for row in all_values[1:]:
                if len(row) <= PAYMENT_DATE_COL:
                    continue
                payment_type = row[PAYMENT_TYPE_COL].strip()
                payment_date = row[PAYMENT_DATE_COL].strip()
                if payment_type == PAYMENT_TYPE_VALUE and payment_date == target_str:
                    total += 1

        logger.info("Upsales purchases on %s: %d", target_date, total)
        return total

    # ------------------------------------------------------------------
    # Weekly plan sheet helpers
    # ------------------------------------------------------------------

    def _set_date_range(self, ws: gspread.Worksheet, target_date: date) -> None:
        """Set A2 and B2 to target_date so the sheet recalculates."""
        date_str = target_date.strftime("%d.%m.%Y")
        ws.update(f"{DATE_FROM_COL}2", [[date_str]])
        ws.update(f"{DATE_TO_COL}2", [[date_str]])
        logger.debug("Set date range A2:B2 to %s", date_str)

    @staticmethod
    def _parse_number(value) -> float:
        """Parse a cell value that may be formatted as a number string."""
        if value is None or value == "":
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = str(value).replace(" ", "").replace("\u00a0", "").replace(",", ".").strip()
        try:
            return float(cleaned)
        except ValueError:
            logger.warning("Could not parse number from: %r", value)
            return 0.0

    def get_new_sales_revenue(self, spreadsheet_id: str, target_date: date, sheet_name: str = "План еженедельный") -> float:
        """
        Set date in the weekly plan sheet, then return D47 + D48.
        """
        ss = self._open_spreadsheet(spreadsheet_id)
        try:
            ws = ss.worksheet(sheet_name)
        except WorksheetNotFound:
            available = [w.title for w in ss.worksheets()]
            logger.error("Sheet %r not found. Available sheets: %s", sheet_name, available)
            raise
        self._set_date_range(ws, target_date)

        d47 = self._parse_number(ws.acell(f"{REVENUE_COL}{NEW_SALES_REV_ROW_START}").value)
        d48 = self._parse_number(ws.acell(f"{REVENUE_COL}{NEW_SALES_REV_ROW_END}").value)
        total = d47 + d48
        logger.info("New sales revenue on %s: %.2f (D47=%.2f, D48=%.2f)", target_date, total, d47, d48)
        return total

    def get_upsales_revenue(self, spreadsheet_id: str, target_date: date, sheet_name: str = "План еженедельный") -> float:
        """
        Set date in the weekly plan sheet, then return D49 + D50.
        """
        ss = self._open_spreadsheet(spreadsheet_id)
        ws = ss.worksheet(sheet_name)
        self._set_date_range(ws, target_date)

        d49 = self._parse_number(ws.acell(f"{REVENUE_COL}{UPSALES_REV_ROW_START}").value)
        d50 = self._parse_number(ws.acell(f"{REVENUE_COL}{UPSALES_REV_ROW_END}").value)
        total = d49 + d50
        logger.info("Upsales revenue on %s: %.2f (D49=%.2f, D50=%.2f)", target_date, total, d49, d50)
        return total

    # ------------------------------------------------------------------
    # Daily report sheet writer
    # ------------------------------------------------------------------

    def write_daily_report(
        self,
        report_spreadsheet_id: str,
        report_sheet_name: str,
        target_date: date,
        new_sales_1d3d: int,
        new_sales_total: int,
        expires: int,
        upsales_purch: int,
        new_sales_revenue: float,
        upsales_revenue: float,
        city_col: str = "A",
        date_col: str = "B",
        city_value: str = "Астана",
    ) -> bool:
        """
        Find the row in the report sheet matching city=Астана and target_date,
        then write values to columns R-W.

        Layout assumed:
          A  = city name
          B  = date (dd.mm or dd.mm.yyyy)
          R  = new_sales_1d3d
          S  = new_sales_total
          T  = expires
          U  = upsales_purch
          V  = new_sales_revenue
          W  = upsales_revenue

        Returns True if row was found and updated.
        """
        ss = self._open_spreadsheet(report_spreadsheet_id)
        ws = ss.worksheet(report_sheet_name)

        all_values = ws.get_all_values()

        # Build possible date strings to match
        date_short = target_date.strftime("%-d.%-m")          # e.g. "25.2"
        date_short2 = target_date.strftime("%d.%m")           # e.g. "25.02"
        date_long = target_date.strftime("%d.%m.%Y")          # e.g. "25.02.2026"
        date_candidates = {date_short, date_short2, date_long}

        city_col_idx = _col_letter_to_idx(city_col)
        date_col_idx = _col_letter_to_idx(date_col)

        target_row_idx = None  # 0-based

        for i, row in enumerate(all_values):
            city_cell = row[city_col_idx].strip() if len(row) > city_col_idx else ""
            date_cell = row[date_col_idx].strip() if len(row) > date_col_idx else ""

            city_matches = city_cell == city_value
            date_matches = date_cell in date_candidates

            if city_matches and date_matches:
                target_row_idx = i
                break

        if target_row_idx is None:
            logger.error(
                "Could not find row for city=%s, date=%s in sheet '%s'",
                city_value, target_date, report_sheet_name,
            )
            logger.error("Looking for date strings: %s", date_candidates)
            logger.error("First 15 rows (col %s, col %s):", city_col, date_col)
            for i, row in enumerate(all_values[:15]):
                a = row[city_col_idx].strip() if len(row) > city_col_idx else "<empty>"
                b = row[date_col_idx].strip() if len(row) > date_col_idx else "<empty>"
                logger.error("  row %d: city=%r  date=%r", i + 1, a, b)
            return False

        # gspread uses 1-based row numbers
        sheet_row = target_row_idx + 1

        updates = [
            ("R", new_sales_1d3d),
            ("S", new_sales_total),
            ("T", expires),
            ("U", upsales_purch),
            ("V", new_sales_revenue),
            ("W", upsales_revenue),
        ]

        for col_letter, value in updates:
            ws.update(f"{col_letter}{sheet_row}", [[value]])
            logger.debug("Wrote %s to %s%d", value, col_letter, sheet_row)

        logger.info(
            "Updated row %d for %s / %s: R=%s S=%s T=%s U=%s V=%s W=%s",
            sheet_row, city_value, target_date,
            new_sales_1d3d, new_sales_total, expires,
            upsales_purch, new_sales_revenue, upsales_revenue,
        )
        return True


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _col_letter_to_idx(letter: str) -> int:
    """Convert column letter (A, B, ... Z, AA, ...) to 0-based index."""
    letter = letter.upper()
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1
