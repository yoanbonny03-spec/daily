"""
Google Sheets client for reading payment data and writing daily report.
"""

import json
import logging
import os
import time
from datetime import date
from typing import Optional

import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Column indices (0-based) in payment employee sheets
PAYMENT_TYPE_COL = 5   # Column F
PAYMENT_DATE_COL = 9   # Column J
PAYMENT_TYPE_VALUE = "Повторные продажи"

KASPI_TYPE_COL   = 6   # Column G  — "Рассрочка"
KASPI_AMOUNT_COL = 1   # Column B  — payment amount
KASPI_DATE_COL   = 9   # Column J  — date
KASPI_TYPE_VALUE = "Рассрочка"

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


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _retry(fn, *args, max_retries: int = 5, **kwargs):
    """Call fn(*args, **kwargs), retrying on HTTP 429 with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except APIError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 429:
                wait = (2 ** attempt) * 5  # 5, 10, 20, 40, 80 s
                logger.warning(
                    "Sheets quota 429, retry in %ds (attempt %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Sheets API: 429 quota exceeded after {max_retries} retries")


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
        return _retry(self.gc.open_by_key, spreadsheet_id)

    def get_payment_figures(
        self, spreadsheet_id: str, target_date: date
    ) -> tuple[int, float]:
        """Return (upsales_purchases, kaspi_revenue) in a single pass over employee sheets.

        Reads each employee worksheet exactly once instead of twice.
        """
        ss = self._open_spreadsheet(spreadsheet_id)
        target_str = target_date.strftime("%d.%m.%Y")
        upsales_count = 0
        kaspi_total = 0.0

        for ws in _retry(ss.worksheets):
            if ws.title in NON_EMPLOYEE_SHEETS:
                continue
            try:
                all_values = _retry(ws.get_all_values)
            except Exception as exc:
                logger.warning("Could not read sheet %s: %s", ws.title, exc)
                continue

            for row in all_values[1:]:  # skip header
                if len(row) <= PAYMENT_DATE_COL:
                    continue
                payment_date = row[PAYMENT_DATE_COL].strip()
                if payment_date != target_str:
                    continue
                # Upsales purchase
                if row[PAYMENT_TYPE_COL].strip() == PAYMENT_TYPE_VALUE:
                    upsales_count += 1
                # Kaspi revenue
                if row[KASPI_TYPE_COL].strip() == KASPI_TYPE_VALUE:
                    kaspi_total += self._parse_number(row[KASPI_AMOUNT_COL])

        logger.info("Upsales purchases on %s: %d", target_date, upsales_count)
        logger.info("Kaspi revenue on %s: %.2f", target_date, kaspi_total)
        return upsales_count, kaspi_total

    # ------------------------------------------------------------------
    # Weekly plan sheet helpers
    # ------------------------------------------------------------------

    def _set_date_range(self, ws: gspread.Worksheet, target_date: date) -> None:
        """Set A2 and B2 to target_date so the sheet recalculates (single API call)."""
        date_str = target_date.strftime("%d.%m.%Y")
        _retry(ws.update, "A2:B2", [[date_str, date_str]])
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

    def get_revenue_figures(
        self,
        spreadsheet_id: str,
        target_date: date,
        sheet_name: str = "План еженедельный",
    ) -> tuple[float, float]:
        """Return (new_sales_revenue, upsales_revenue) in a single spreadsheet session.

        Opens the worksheet once, sets the date once (A2:B2), and reads D47:D50 in one call.
        """
        ss = self._open_spreadsheet(spreadsheet_id)
        try:
            ws = _retry(ss.worksheet, sheet_name)
        except WorksheetNotFound:
            available = [w.title for w in _retry(ss.worksheets)]
            logger.error("Sheet %r not found. Available: %s", sheet_name, available)
            raise

        self._set_date_range(ws, target_date)

        # Read D47:D50 in one request
        raw = _retry(ws.get_values, f"D{NEW_SALES_REV_ROW_START}:D{UPSALES_REV_ROW_END}")
        # raw is [[d47], [d48], [d49], [d50]]  (may be shorter if cells are empty)
        def _cell(row_idx: int) -> float:
            try:
                return self._parse_number(raw[row_idx][0])
            except (IndexError, TypeError):
                return 0.0

        d47, d48, d49, d50 = _cell(0), _cell(1), _cell(2), _cell(3)
        new_sales_rev = d47 + d48
        upsales_rev   = d49 + d50

        logger.info(
            "New sales revenue on %s: %.2f (D47=%.2f, D48=%.2f)",
            target_date, new_sales_rev, d47, d48,
        )
        logger.info(
            "Upsales revenue on %s: %.2f (D49=%.2f, D50=%.2f)",
            target_date, upsales_rev, d49, d50,
        )
        return new_sales_rev, upsales_rev

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
        kaspi_revenue: float = 0.0,
        city_col: str = "A",
        date_col: str = "B",
        city_value: str = "Астана",
    ) -> bool:
        """
        Find the row in the report sheet matching city=Астана and target_date,
        then write values to columns R-W and Y in a single batch_update call.

        Layout assumed:
          A  = city name
          B  = date (dd.mm or dd.mm.yyyy)
          R  = new_sales_1d3d
          S  = new_sales_total
          T  = expires
          U  = upsales_purch
          V  = new_sales_revenue
          W  = upsales_revenue
          Y  = kaspi_revenue

        Returns True if row was found and updated.
        """
        ss = self._open_spreadsheet(report_spreadsheet_id)
        ws = _retry(ss.worksheet, report_sheet_name)

        all_values = _retry(ws.get_all_values)

        # Build possible date strings to match
        date_short = target_date.strftime("%-d.%-m")          # e.g. "25.2"
        date_short2 = target_date.strftime("%d.%m")           # e.g. "25.02"
        date_long = target_date.strftime("%d.%m.%Y")          # e.g. "25.02.2026"
        _months_en = {
            1: "January", 2: "February", 3: "March", 4: "April",
            5: "May", 6: "June", 7: "July", 8: "August",
            9: "September", 10: "October", 11: "November", 12: "December",
        }
        date_en = f"{_months_en[target_date.month]} {target_date.day}"  # e.g. "February 26"
        date_candidates = {date_short, date_short2, date_long, date_en}

        city_col_idx = _col_letter_to_idx(city_col)
        date_col_idx = _col_letter_to_idx(date_col)

        target_row_idx = None  # 0-based
        last_city = ""  # carry-forward for merged city cells

        for i, row in enumerate(all_values):
            city_cell = row[city_col_idx].strip() if len(row) > city_col_idx else ""
            if city_cell:
                last_city = city_cell
            date_cell = row[date_col_idx].strip() if len(row) > date_col_idx else ""

            if last_city == city_value and date_cell in date_candidates:
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

        # Write all 7 columns in a single batch_update (1 API call instead of 7)
        _retry(
            ws.batch_update,
            [
                {"range": f"R{sheet_row}", "values": [[new_sales_1d3d]]},
                {"range": f"S{sheet_row}", "values": [[new_sales_total]]},
                {"range": f"T{sheet_row}", "values": [[expires]]},
                {"range": f"U{sheet_row}", "values": [[upsales_purch]]},
                {"range": f"V{sheet_row}", "values": [[new_sales_revenue]]},
                {"range": f"W{sheet_row}", "values": [[upsales_revenue]]},
                {"range": f"Y{sheet_row}", "values": [[kaspi_revenue]]},
            ],
        )

        logger.info(
            "Updated row %d for %s / %s: R=%s S=%s T=%s U=%s V=%s W=%s Y=%s",
            sheet_row, city_value, target_date,
            new_sales_1d3d, new_sales_total, expires,
            upsales_purch, new_sales_revenue, upsales_revenue, kaspi_revenue,
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
