"""
AmoCRM API client for fetching sales and lead data.
"""

import time
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Astana is UTC+5
ASTANA_TZ = timezone(timedelta(hours=5))


def _day_bounds_astana(target_date: date) -> tuple[int, int]:
    """
    Return (day_start, day_end) as unix timestamps for the given date
    in Astana local time (UTC+5).
    """
    day_start = int(datetime(target_date.year, target_date.month, target_date.day,
                              0, 0, 0, tzinfo=ASTANA_TZ).timestamp())
    day_end   = int(datetime(target_date.year, target_date.month, target_date.day,
                              23, 59, 59, tzinfo=ASTANA_TZ).timestamp())
    return day_start, day_end


class AmoCRMClient:
    """Client for AmoCRM REST API v4."""

    def __init__(self, domain: str, access_token: str):
        self.base_url = f"https://{domain}.amocrm.ru/api/v4"
        self._access_token = access_token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def _new_session(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: dict = None) -> dict:
        """
        GET request. Builds query string with literal brackets (not %-encoded)
        because AmoCRM rejects %5B%5D-encoded bracket params.
        Retries up to 3 times on ConnectionError with exponential backoff.
        """
        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{url}?{qs}"
        else:
            full_url = url

        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                resp = self.session.get(full_url, timeout=30)
                break
            except requests.exceptions.ConnectionError as exc:
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning("ConnectionError (attempt %d/%d), retrying in %ds: %s",
                                   attempt + 1, max_retries, wait, exc)
                    time.sleep(wait)
                    self._new_session()
                else:
                    raise

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            logger.warning("Rate limited, sleeping %s s", retry_after)
            time.sleep(retry_after)
            resp = self.session.get(full_url, timeout=30)

        if not resp.ok:
            logger.error("AmoCRM API %d | URL: %s | Body: %s",
                         resp.status_code, resp.url, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, params: dict, max_pages: int = None) -> list:
        """Fetch all pages for a paginated endpoint."""
        results = []
        page = 1
        while True:
            params["page"] = page
            params["limit"] = 250
            try:
                data = self._get(path, params)
            except requests.HTTPError as exc:
                if exc.response.status_code == 204:
                    break
                raise
            items = data.get("_embedded", {})
            key = next(iter(items), None)
            if not key:
                break
            batch = items[key]
            results.extend(batch)
            if len(batch) < 250:
                break
            if max_pages and page >= max_pages:
                logger.warning("Reached max_pages=%d (%d leads). Some results may be missing.",
                               max_pages, len(results))
                break
            page += 1
        return results

    # ------------------------------------------------------------------
    # Custom fields helpers
    # ------------------------------------------------------------------

    def get_lead_fields(self) -> dict:
        """Return dict of field_name -> field_id for leads."""
        data = self._get("/leads/custom_fields")
        fields = {}
        for f in data.get("_embedded", {}).get("custom_fields", []):
            fields[f["name"]] = f["id"]
        return fields

    # ------------------------------------------------------------------
    # Pipelines / statuses
    # ------------------------------------------------------------------

    def get_pipelines(self) -> list:
        data = self._get("/leads/pipelines")
        return data.get("_embedded", {}).get("pipelines", [])

    def get_pipeline_map(self) -> dict:
        return {p["name"]: p["id"] for p in self.get_pipelines()}

    def get_status_map(self) -> dict:
        result = {}
        for p in self.get_pipelines():
            statuses = {s["name"]: s["id"] for s in p.get("_embedded", {}).get("statuses", [])}
            result[p["id"]] = statuses
        return result

    # ------------------------------------------------------------------
    # Core lead fetchers
    # ------------------------------------------------------------------

    def get_leads(
        self,
        closed_at_from: int,
        closed_at_to: int,
        pipeline_ids: list[int] = None,
        status_ids: list[int] = None,
    ) -> list:
        params: dict = {
            "filter[closed_at][from]": closed_at_from,
            "filter[closed_at][to]": closed_at_to,
        }
        if pipeline_ids:
            for i, pid in enumerate(pipeline_ids):
                params[f"filter[pipeline_id][{i}]"] = pid
        if status_ids:
            for i, sid in enumerate(status_ids):
                params[f"filter[statuses][{i}][status_id]"] = sid
        return self._paginate("/leads", params)

    def get_leads_by_date_field(
        self,
        field_id: int,
        date_from: int,
        date_to: int,
        pipeline_ids: list[int] = None,
        status_ids: list[int] = None,
        closed_at_from: int = None,
        closed_at_to: int = None,
        enum_filters: dict = None,
    ) -> list:
        """
        Fetch leads, optionally filtered by date field and enum custom fields.

        enum_filters — dict {field_id: enum_value_id} для фильтрации по
        кастомным enum-полям (город, отдел) прямо на уровне API.
        """
        base_params: dict = {}
        if pipeline_ids:
            for i, pid in enumerate(pipeline_ids):
                base_params[f"filter[pipeline_id][{i}]"] = pid
        if status_ids:
            for i, sid in enumerate(status_ids):
                base_params[f"filter[statuses][{i}][status_id]"] = sid
        if closed_at_from is not None:
            base_params["filter[closed_at][from]"] = closed_at_from
        if closed_at_to is not None:
            base_params["filter[closed_at][to]"] = closed_at_to
        if enum_filters:
            for cf_id, enum_id in enum_filters.items():
                base_params[f"filter[cf][{cf_id}][0]"] = enum_id

        filter_prefixes = [
            f"filter[cf][{field_id}]",
            f"filter[custom_fields_values][{field_id}]",
        ]
        last_error = None
        for prefix in filter_prefixes:
            params = {
                f"{prefix}[from]": date_from,
                f"{prefix}[to]": date_to,
                **base_params,
            }
            try:
                leads = self._paginate("/leads", params)
                logger.info("Date filter '%s' OK — got %d leads", prefix, len(leads))
                return leads
            except requests.HTTPError as exc:
                if exc.response.status_code == 400:
                    logger.warning("Date filter '%s' → 400, trying next format...", prefix)
                    last_error = exc
                    continue
                raise

        raise RuntimeError(
            f"AmoCRM rejected both date filter formats for field_id={field_id}. "
            f"Last error: {last_error}"
        )

    def get_won_status_ids(self, pipeline_ids: list[int], status_names: list[str]) -> list[int]:
        names_set = set(status_names)
        result = []
        for p in self.get_pipelines():
            if pipeline_ids and p["id"] not in pipeline_ids:
                continue
            for s in p.get("_embedded", {}).get("statuses", []):
                if s["name"] in names_set:
                    result.append(s["id"])
        return result

    # ------------------------------------------------------------------
    # Business logic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_custom_field_value(lead: dict, field_id: int) -> Optional[str]:
        for cfv in lead.get("custom_fields_values") or []:
            if cfv.get("field_id") == field_id:
                vals = cfv.get("values", [])
                if vals:
                    return vals[0].get("value")
        return None

    @staticmethod
    def _get_custom_field_enum_id(lead: dict, field_id: int) -> Optional[int]:
        """Return the enum_id of a select/radio custom field value."""
        for cfv in lead.get("custom_fields_values") or []:
            if cfv.get("field_id") == field_id:
                vals = cfv.get("values", [])
                if vals:
                    return vals[0].get("enum_id")
        return None

    @staticmethod
    def _field_matches_date(field_value, day_start: int, day_end: int) -> bool:
        if field_value is None:
            return False
        try:
            ts = int(field_value)
            return day_start <= ts <= day_end
        except (ValueError, TypeError):
            return False

    def count_new_sales(
        self,
        target_date: date,
        pipeline_ids: list[int],
        contract_date_field_id: int,
        city_field_id: int,
        city_enum_id: int,
        dept_field_id: int,
        dept_enum_id: int,
    ) -> tuple[int, int]:
        """
        Returns (count_1d_3d, count_total) for leads where
        дата_заключения_договора = target_date, город=Астана, отдел=Offline.

        Фильтр по городу и отделу применяется на уровне API через enum ID
        (аналогично фильтру в AmoCRM UI). Дата договора проверяется в Python,
        т.к. AmoCRM игнорирует filter[cf][date_field][from/to].
        """
        day_start, day_end = _day_bounds_astana(target_date)
        logger.info("count_new_sales: date=%s, pipeline_ids=%s", target_date, pipeline_ids)

        # closed_at ограничивает выборку на уровне API (надёжный стандартный фильтр).
        # AmoCRM игнорирует filter[cf][date_field][from/to] и enum-фильтры,
        # поэтому фильтрацию по городу, отделу и дате договора делаем в Python.
        leads = self.get_leads_by_date_field(
            field_id=contract_date_field_id,
            date_from=day_start,
            date_to=day_end,
            pipeline_ids=pipeline_ids,
            closed_at_from=day_start,
            closed_at_to=day_end,
        )

        logger.info("count_new_sales: leads from API = %d, filtering by city/dept/contract_date in Python", len(leads))
        count_total = 0
        count_1d_3d = 0
        threshold = timedelta(days=3)

        for lead in leads:
            # Фильтр по городу и отделу через enum_id — надёжнее текстового сравнения
            if self._get_custom_field_enum_id(lead, city_field_id) != city_enum_id:
                continue
            if self._get_custom_field_enum_id(lead, dept_field_id) != dept_enum_id:
                continue
            # Проверяем дату договора (API-фильтр по ней не работает)
            contract_date = self._get_custom_field_value(lead, contract_date_field_id)
            if not self._field_matches_date(contract_date, day_start, day_end):
                continue

            count_total += 1
            created_at = lead.get("created_at")
            if created_at:
                delta = target_date - datetime.fromtimestamp(created_at, tz=ASTANA_TZ).date()
                if delta <= threshold:
                    count_1d_3d += 1

        return count_1d_3d, count_total

    def count_expires(
        self,
        target_date: date,
        end_date_field_id: int,
        pipeline_ids: list[int],
        city_field_id: int,
        city_enum_id: int,
        dept_field_id: int,
        dept_enum_id: int,
    ) -> int:
        """
        Count active leads where дата_окончания_занятий = target_date,
        город=Астана, отдел=Offline.

        Фильтр по городу и отделу применяется на уровне API через enum ID.
        Дата окончания проверяется в Python.
        """
        day_start, day_end = _day_bounds_astana(target_date)
        logger.info("count_expires: date=%s, pipeline_ids=%s", target_date, pipeline_ids)

        leads = self.get_leads_by_date_field(
            field_id=end_date_field_id,
            date_from=day_start,
            date_to=day_end,
            pipeline_ids=pipeline_ids,
            enum_filters={city_field_id: city_enum_id, dept_field_id: dept_enum_id},
        )

        logger.info("count_expires: leads from API = %d, filtering by city/dept/end_date in Python", len(leads))
        count = 0
        for lead in leads:
            if self._get_custom_field_enum_id(lead, city_field_id) != city_enum_id:
                continue
            if self._get_custom_field_enum_id(lead, dept_field_id) != dept_enum_id:
                continue
            end_date = self._get_custom_field_value(lead, end_date_field_id)
            if self._field_matches_date(end_date, day_start, day_end):
                count += 1

        return count
