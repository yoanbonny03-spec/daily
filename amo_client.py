"""
AmoCRM API client for fetching sales and lead data.

Аутентификация:
  - AMO_ACCESS_TOKEN = "Долгосрочный токен" из настроек интеграции amoCRM
    (Настройки → Интеграции → ваша интеграция → вкладка "Ключи и токены")
  - Долгосрочный токен используется напрямую как Bearer token
  - При истечении (401) токен нужно обновить вручную в amoCRM и заменить в Railway
  - Опционально: если заданы AMO_CLIENT_ID/SECRET/REDIRECT_URI/REFRESH_TOKEN —
    клиент попробует автоматически обновить токен через OAuth2 refresh flow
"""

import json
import time
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Astana is UTC+5
ASTANA_TZ = timezone(timedelta(hours=5))

TOKENS_FILE = Path(__file__).parent / "tokens.json"

# Local cache for subscription pipeline leads.
# Stores {str(lead_id): {"end_date": ts|None, "city": enum_id|None, "dept": enum_id|None}}
# so daily runs only fetch leads updated since the last sync.
SUBSCRIPTION_CACHE_FILE = Path(__file__).parent / "subscription_cache.json"
SUBSCRIPTION_CACHE_MAX_AGE_DAYS = 7  # force full resync after this many days


def _load_subscription_cache() -> dict:
    if SUBSCRIPTION_CACHE_FILE.exists():
        try:
            return json.loads(SUBSCRIPTION_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_subscription_cache(cache: dict) -> None:
    SUBSCRIPTION_CACHE_FILE.write_text(json.dumps(cache))


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


def _month_bounds_astana(target_date: date) -> tuple[int, int]:
    """
    Return (month_start, month_end) as unix timestamps for the whole month
    of target_date in Astana local time (UTC+5).
    """
    month_start = int(datetime(target_date.year, target_date.month, 1,
                               0, 0, 0, tzinfo=ASTANA_TZ).timestamp())
    if target_date.month == 12:
        next_month = datetime(target_date.year + 1, 1, 1, 0, 0, 0, tzinfo=ASTANA_TZ)
    else:
        next_month = datetime(target_date.year, target_date.month + 1, 1, 0, 0, 0, tzinfo=ASTANA_TZ)
    month_end = int(next_month.timestamp()) - 1
    return month_start, month_end


def _load_tokens() -> dict:
    """Load saved tokens from tokens.json."""
    if TOKENS_FILE.exists():
        try:
            return json.loads(TOKENS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_tokens(tokens: dict) -> None:
    """Persist tokens to tokens.json."""
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    logger.debug("Tokens saved to %s", TOKENS_FILE)


class AmoCRMClient:
    """Client for AmoCRM REST API v4.

    Использует Долгосрочный токен как Bearer access_token.
    При наличии OAuth2 credentials (client_id/secret/refresh_token) —
    автоматически обновляет токен при 401.
    """

    def __init__(
        self,
        domain: str,
        access_token: str,
        client_id: str = None,
        client_secret: str = None,
        redirect_uri: str = None,
        refresh_token: str = None,
    ):
        self.domain = domain
        self.base_url = f"https://{domain}.amocrm.ru/api/v4"
        self._oauth_url = f"https://{domain}.amocrm.ru/oauth2/access_token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

        # Prefer saved tokens from previous refresh, fall back to provided value
        saved = _load_tokens()
        self._access_token = saved.get("access_token") or access_token
        self._refresh_token = saved.get("refresh_token") or refresh_token

        self._build_session()

    def _build_session(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        })

    def _refresh(self) -> None:
        """Exchange refresh_token for a new access_token + refresh_token."""
        if not all([self._client_id, self._client_secret, self._redirect_uri, self._refresh_token]):
            raise RuntimeError(
                "AmoCRM access token expired (401). "
                "Обнови Долгосрочный токен в amoCRM (Настройки → Интеграции → "
                "ваша интеграция → Ключи и токены) и обнови AMO_ACCESS_TOKEN в Railway."
            )
        logger.info("Refreshing AmoCRM access token via refresh_token...")
        resp = requests.post(
            self._oauth_url,
            json={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "redirect_uri": self._redirect_uri,
            },
            timeout=30,
        )
        if not resp.ok:
            logger.error("Token refresh failed %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()

        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]

        _save_tokens({
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
        })
        logger.info("Access token refreshed successfully.")
        self._build_session()

    def _new_session(self) -> None:
        self._build_session()

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

        if resp.status_code == 401:
            logger.warning("Got 401, refreshing token and retrying...")
            self._refresh()
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
            logger.info("_paginate %s page=%d batch=%d total=%d", path, page, len(batch), len(results))
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
        if status_ids and pipeline_ids:
            idx = 0
            for pid in pipeline_ids:
                for sid in status_ids:
                    params[f"filter[statuses][{idx}][pipeline_id]"] = pid
                    params[f"filter[statuses][{idx}][status_id]"] = sid
                    idx += 1
        elif status_ids:
            for i, sid in enumerate(status_ids):
                params[f"filter[statuses][{i}][status_id]"] = sid
        return self._paginate("/leads", params)

    def get_leads_by_status(
        self,
        pipeline_ids: list[int],
        status_ids: list[int],
    ) -> list:
        """
        Fetch active leads filtered only by pipeline + status (no closed_at).
        Use when stages are intermediate (not AmoCRM 'won'), so closed_at is not set.
        """
        params: dict = {}
        if pipeline_ids:
            for i, pid in enumerate(pipeline_ids):
                params[f"filter[pipeline_id][{i}]"] = pid
        if status_ids and pipeline_ids:
            idx = 0
            for pid in pipeline_ids:
                for sid in status_ids:
                    params[f"filter[statuses][{idx}][pipeline_id]"] = pid
                    params[f"filter[statuses][{idx}][status_id]"] = sid
                    idx += 1
        elif status_ids:
            for i, sid in enumerate(status_ids):
                params[f"filter[statuses][{i}][status_id]"] = sid
        return self._paginate("/leads", params)

    def get_leads_by_pipeline_and_enum(
        self,
        pipeline_ids: list[int],
        enum_filters: dict = None,
    ) -> list:
        """
        Fetch all leads filtered by pipeline + optional enum custom fields.
        Used when AmoCRM ignores date field filters (e.g. field 89203 in
        subscription pipelines) — date matching is done in Python instead.
        """
        params: dict = {}
        if pipeline_ids:
            for i, pid in enumerate(pipeline_ids):
                params[f"filter[pipeline_id][{i}]"] = pid
        if enum_filters:
            for cf_id, enum_id in enum_filters.items():
                params[f"filter[cf][{cf_id}][]"] = enum_id
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
        updated_at_from: int = None,
        updated_at_to: int = None,
        enum_filters: dict = None,
        max_pages: int = None,
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
        # AmoCRM v4: statuses filter requires BOTH pipeline_id AND status_id per group.
        # Without pipeline_id the filter is silently ignored and all leads are returned.
        if status_ids and pipeline_ids:
            idx = 0
            for pid in pipeline_ids:
                for sid in status_ids:
                    base_params[f"filter[statuses][{idx}][pipeline_id]"] = pid
                    base_params[f"filter[statuses][{idx}][status_id]"] = sid
                    idx += 1
        if closed_at_from is not None:
            base_params["filter[closed_at][from]"] = closed_at_from
        if closed_at_to is not None:
            base_params["filter[closed_at][to]"] = closed_at_to
        if updated_at_from is not None:
            base_params["filter[updated_at][from]"] = updated_at_from
        if updated_at_to is not None:
            base_params["filter[updated_at][to]"] = updated_at_to
        if enum_filters:
            for cf_id, enum_id in enum_filters.items():
                base_params[f"filter[cf][{cf_id}][]"] = enum_id

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
                leads = self._paginate("/leads", params, max_pages=max_pages)
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

    def get_lost_status_ids(self, pipeline_ids: list[int]) -> set[int]:
        """Return status IDs of type=3 (lost/rejected) for the given pipelines."""
        result = set()
        for p in self.get_pipelines():
            if pipeline_ids and p["id"] not in pipeline_ids:
                continue
            for s in p.get("_embedded", {}).get("statuses", []):
                if s.get("type") == 3:
                    result.add(s["id"])
        return result

    def get_open_status_ids(self, pipeline_ids: list[int]) -> list[int]:
        """Return status IDs for active (non-terminal) stages in given pipelines.

        Excludes type=3 (lost), type=142 (won), type=143 (lost/fail) — i.e. keeps
        only the regular in-progress stages. Used to avoid downloading historical
        closed leads when only active subscriptions are needed.
        """
        terminal_types = {3, 142, 143}
        result = []
        for p in self.get_pipelines():
            if pipeline_ids and p["id"] not in pipeline_ids:
                continue
            for s in p.get("_embedded", {}).get("statuses", []):
                if s.get("type") not in terminal_types:
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
        won_status_ids: list[int],
        city_field_id: int,
        city_enum_id: int,
        dept_field_id: int,
        dept_enum_id: int,
    ) -> tuple[int, int]:
        """
        Returns (count_1d_3d, count_total) for leads where:
          - воронка = sales pipelines
          - город = Астана, отдел = Offline (проверка Python через enum_id)
          - дата заключения договора = target_date (проверка Python)
          - этап сделки — один из 7 стадий (проверка Python через status_id)

        count_1d_3d: |дата_договора − дата_создания_лида| ≤ 3 дня.
        Фильтр по дате договора выполняется на уровне API, остальное — в Python.
        """
        day_start, day_end = _day_bounds_astana(target_date)
        logger.info("count_new_sales: date=%s, pipeline_ids=%s, won_statuses=%s",
                    target_date, pipeline_ids, won_status_ids)

        # Фильтруем на уровне API:
        #   1. pipeline + statuses (работает надёжно)
        #   2. updated_at >= start of target_date — сделка с датой договора = today
        #      была обновлена именно сегодня; отсекаем весь исторический хвост
        #   3. custom date field filter (может игнорироваться amoCRM, но пробуем)
        # Итоговый Python-фильтр по дате договора страхует от false positives.
        leads = self.get_leads_by_date_field(
            field_id=contract_date_field_id,
            date_from=day_start,
            date_to=day_end,
            pipeline_ids=pipeline_ids,
            status_ids=won_status_ids,
            updated_at_from=day_start,
        )

        logger.info("count_new_sales: leads from API = %d, filtering city/dept/status/contract_date in Python", len(leads))
        count_total = 0
        count_1d_3d = 0
        won_set = set(won_status_ids)
        threshold = timedelta(days=3)

        for lead in leads:
            # Город = Астана
            if self._get_custom_field_enum_id(lead, city_field_id) != city_enum_id:
                continue
            # Отдел = Offline
            if self._get_custom_field_enum_id(lead, dept_field_id) != dept_enum_id:
                continue
            # Дата заключения договора = target_date
            contract_date = self._get_custom_field_value(lead, contract_date_field_id)
            if not self._field_matches_date(contract_date, day_start, day_end):
                continue
            # Этап сделки — один из 7 нужных стадий
            if lead.get("status_id") not in won_set:
                logger.debug("lead %s: status_id=%s не в won_set, пропуск", lead.get("id"), lead.get("status_id"))
                continue

            count_total += 1

            # 1d-3d: дата создания лида ≤ 3 дней до даты договора
            created_at = lead.get("created_at")
            if created_at:
                try:
                    created_date = datetime.fromtimestamp(int(created_at), tz=ASTANA_TZ).date()
                    delta = target_date - created_date
                    if timedelta(0) <= delta <= threshold:
                        count_1d_3d += 1
                except (ValueError, TypeError, OSError):
                    logger.warning("lead %s: bad created_at value %r", lead.get("id"), created_at)

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

        Использует локальный кеш subscription_cache.json:
          - Первый запуск (или раз в 7 дней): полный fetch всех ~28k лидов.
          - Последующие запуски: только лиды с updated_at >= последней синхронизации
            (filter[updated_at][from] работает, это системное поле AmoCRM).
            Обычно 50-300 лидов = 1-2 страницы = секунды.
        Подсчёт выполняется из кеша в памяти.
        """
        day_start, day_end = _day_bounds_astana(target_date)

        # ── Build base pipeline params ───────────────────────────────────────
        pipeline_params: dict = {}
        for i, pid in enumerate(pipeline_ids):
            pipeline_params[f"filter[pipeline_id][{i}]"] = pid

        # ── Load cache ───────────────────────────────────────────────────────
        cache = _load_subscription_cache()
        last_sync: float = cache.get("_last_sync", 0.0)
        leads_cache: dict = cache.get("leads", {})
        cache_age_days = (time.time() - last_sync) / 86400

        if cache_age_days > SUBSCRIPTION_CACHE_MAX_AGE_DAYS or not leads_cache:
            # ── Full sync ────────────────────────────────────────────────────
            logger.info(
                "count_expires: full cache sync (age=%.1f days, %d leads cached)",
                cache_age_days, len(leads_cache),
            )
            all_leads = self._paginate("/leads", pipeline_params)
            leads_cache = {}
            for lead in all_leads:
                leads_cache[str(lead["id"])] = {
                    "end_date": self._get_custom_field_value(lead, end_date_field_id),
                    "city":     self._get_custom_field_enum_id(lead, city_field_id),
                    "dept":     self._get_custom_field_enum_id(lead, dept_field_id),
                }
            logger.info("count_expires: full sync done, %d leads cached", len(leads_cache))
        else:
            # ── Incremental sync: only leads updated since last sync ──────────
            # Subtract 1 hour buffer to avoid missing leads at boundary.
            updated_from = int(last_sync) - 3600
            inc_params = {**pipeline_params, "filter[updated_at][from]": updated_from}
            updated_leads = self._paginate("/leads", inc_params)
            for lead in updated_leads:
                leads_cache[str(lead["id"])] = {
                    "end_date": self._get_custom_field_value(lead, end_date_field_id),
                    "city":     self._get_custom_field_enum_id(lead, city_field_id),
                    "dept":     self._get_custom_field_enum_id(lead, dept_field_id),
                }
            logger.info(
                "count_expires: incremental sync, %d leads updated, %d total cached",
                len(updated_leads), len(leads_cache),
            )

        # ── Persist updated cache ────────────────────────────────────────────
        _save_subscription_cache({"_last_sync": time.time(), "leads": leads_cache})

        # ── Count from cache ─────────────────────────────────────────────────
        count = 0
        count_date_only = 0
        passed_city = 0
        passed_dept = 0
        sample_matches: list = []

        for data in leads_cache.values():
            date_ok = self._field_matches_date(data.get("end_date"), day_start, day_end)
            if date_ok:
                count_date_only += 1

            if data.get("city") != city_enum_id:
                continue
            passed_city += 1

            if data.get("dept") != dept_enum_id:
                continue
            passed_dept += 1

            if date_ok:
                count += 1
                if len(sample_matches) < 5:
                    sample_matches.append(data.get("end_date"))

        logger.info(
            "count_expires: date=%s date_only=%d | city=%d dept=%d | matched=%d",
            target_date, count_date_only, passed_city, passed_dept, count,
        )
        if sample_matches:
            logger.info("count_expires: sample matched end_date timestamps: %s", sample_matches)

        # Fallback: if city/dept filter yields 0 but date matched something,
        # enum IDs may have changed — return date-only count and warn.
        if count == 0 and count_date_only > 0:
            logger.warning(
                "count_expires: city/dept filter returned 0 (city_enum=%d dept_enum=%d) "
                "but date matched %d leads — returning date-only count as fallback.",
                city_enum_id, dept_enum_id, count_date_only,
            )
            return count_date_only
        return count
