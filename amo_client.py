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
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: dict = None) -> dict:
        """
        GET request. Builds query string with literal brackets (not %-encoded)
        because AmoCRM rejects %5B%5D-encoded bracket params.
        """
        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{url}?{qs}"
        else:
            full_url = url

        resp = self.session.get(full_url, timeout=30)

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
        extra_cf_filters: dict = None,
    ) -> list:
        """
        Fetch leads where a custom date field is within [date_from, date_to].

        extra_cf_filters: {field_id: value} — дополнительные фильтры по кастомным полям,
        например {city_field_id: "Астана", dept_field_id: "Оффлайн"}.
        Добавляются как filter[cf][field_id][]=value в запрос к AmoCRM.

        Tries two AmoCRM date filter formats. If both return 400, raises RuntimeError.
        """
        base_params: dict = {}
        if pipeline_ids:
            for i, pid in enumerate(pipeline_ids):
                base_params[f"filter[pipeline_id][{i}]"] = pid
        if status_ids:
            for i, sid in enumerate(status_ids):
                base_params[f"filter[statuses][{i}][status_id]"] = sid
        if extra_cf_filters:
            for cf_id, cf_val in extra_cf_filters.items():
                base_params[f"filter[cf][{cf_id}][]"] = cf_val

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
            f"Check that AMO_END_DATE_FIELD_ID / AMO_CONTRACT_DATE_FIELD_ID are correct "
            f"(run 'Показать поля AmoCRM' to see actual IDs). Last error: {last_error}"
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
        city_field_id: int,
        department_field_id: int,
        pipeline_ids: list[int],
        contract_date_field_id: int,
        won_status_ids: list[int],
        city_value: str = "Астана",
        department_value: str = "Оффлайн",
    ) -> tuple[int, int]:
        """
        Returns (count_1d_3d, count_total) for leads where
        дата_заключения_договора = target_date, город=Астана, отдел=Оффлайн.
        """
        day_start, day_end = _day_bounds_astana(target_date)
        logger.info(
            "count_new_sales: date=%s, contract_field=%s, day_start=%s, day_end=%s",
            target_date, contract_date_field_id, day_start, day_end,
        )

        leads = self.get_leads_by_date_field(
            field_id=contract_date_field_id,
            date_from=day_start,
            date_to=day_end,
            pipeline_ids=pipeline_ids,
            status_ids=won_status_ids,
            extra_cf_filters={
                city_field_id: city_value,
                department_field_id: department_value,
            },
        )

        count_total = 0
        count_1d_3d = 0
        threshold = timedelta(days=3)

        for lead in leads:
            city = self._get_custom_field_value(lead, city_field_id)
            dept = self._get_custom_field_value(lead, department_field_id)
            if city != city_value or dept != department_value:
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
        city_field_id: int,
        department_field_id: int,
        pipeline_ids: list[int],
        city_value: str = "Астана",
        department_value: str = "Оффлайн",
    ) -> int:
        """
        Count active leads where дата_окончания_занятий = target_date,
        город=Астана, отдел=Оффлайн.
        """
        day_start, day_end = _day_bounds_astana(target_date)
        logger.info(
            "count_expires: date=%s, end_date_field=%s, day_start=%s, day_end=%s",
            target_date, end_date_field_id, day_start, day_end,
        )

        leads = self.get_leads_by_date_field(
            field_id=end_date_field_id,
            date_from=day_start,
            date_to=day_end,
            pipeline_ids=pipeline_ids,
            extra_cf_filters={
                city_field_id: city_value,
                department_field_id: department_value,
            },
        )

        count = 0
        for lead in leads:
            city = self._get_custom_field_value(lead, city_field_id)
            dept = self._get_custom_field_value(lead, department_field_id)
            if city == city_value and dept == department_value:
                count += 1

        return count
