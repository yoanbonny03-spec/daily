"""
AmoCRM API client for fetching sales and lead data.
"""

import time
import logging
from datetime import datetime, date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)


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
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            logger.warning("Rate limited, sleeping %s s", retry_after)
            time.sleep(retry_after)
            resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, params: dict) -> list:
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
            # leads or contacts or pipelines key
            key = next(iter(items), None)
            if not key:
                break
            batch = items[key]
            results.extend(batch)
            if len(batch) < 250:
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
        """Return dict name -> id for pipelines."""
        return {p["name"]: p["id"] for p in self.get_pipelines()}

    def get_status_map(self) -> dict:
        """Return dict pipeline_id -> {status_name: status_id}."""
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
        """
        Fetch leads closed within [closed_at_from, closed_at_to] (unix timestamps).
        Optionally filter by pipeline and status ids.
        """
        params: dict = {
            "filter[closed_at][from]": closed_at_from,
            "filter[closed_at][to]": closed_at_to,
            "with": "contacts,leads",
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
    ) -> list:
        """
        Fetch leads where a custom date field is within [date_from, date_to].
        Used for 'Expires' (end_of_classes date) and new sales (contract date).
        """
        params: dict = {
            f"filter[custom_fields_values][{field_id}][from]": date_from,
            f"filter[custom_fields_values][{field_id}][to]": date_to,
        }
        if pipeline_ids:
            for i, pid in enumerate(pipeline_ids):
                params[f"filter[pipeline_id][{i}]"] = pid
        if status_ids:
            for i, sid in enumerate(status_ids):
                params[f"filter[statuses][{i}][status_id]"] = sid

        return self._paginate("/leads", params)

    def get_won_status_ids(self, pipeline_ids: list[int], status_names: list[str]) -> list[int]:
        """Return status IDs whose names match status_names within the given pipelines."""
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
        Returns (count_1d_3d, count_total) for leads where contract_date = target_date,
        matching city and department filters.

        count_1d_3d: deals where (target_date - created_at) <= 3 days
        count_total: all deals matching filters
        """
        day_start = int(datetime.combine(target_date, datetime.min.time()).timestamp())
        day_end = int(datetime.combine(target_date, datetime.max.time()).timestamp())

        leads = self.get_leads_by_date_field(
            field_id=contract_date_field_id,
            date_from=day_start,
            date_to=day_end,
            pipeline_ids=pipeline_ids,
            status_ids=won_status_ids,
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
                delta = target_date - datetime.fromtimestamp(created_at).date()
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
        Count active leads in given pipelines where end_of_classes = target_date,
        city=Астана, department=Оффлайн.
        """
        day_start = int(datetime.combine(target_date, datetime.min.time()).timestamp())
        day_end = int(datetime.combine(target_date, datetime.max.time()).timestamp())

        leads = self.get_leads_by_date_field(
            field_id=end_date_field_id,
            date_from=day_start,
            date_to=day_end,
            pipeline_ids=pipeline_ids,
        )

        count = 0
        for lead in leads:
            city = self._get_custom_field_value(lead, city_field_id)
            dept = self._get_custom_field_value(lead, department_field_id)
            if city == city_value and dept == department_value:
                count += 1

        return count
