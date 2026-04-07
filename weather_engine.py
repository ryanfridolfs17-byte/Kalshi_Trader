"""
WEATHER FORECAST ENGINE v5.0
================================
Builds weather forecast distributions from NWS and METAR only.

Primary inputs:
  - api.weather.gov point forecast
  - api.weather.gov hourly forecast
  - api.weather.gov grid forecast data
  - aviationweather.gov METAR observations

The runtime path deliberately avoids Open-Meteo so Railway shared-IP
quotas cannot blind the bot.
"""

import math
import re
import threading
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import config


# ---------------------------------------------------------
# CITY CONFIGURATION
# ---------------------------------------------------------
# Coordinates for the actual NWS stations Kalshi uses
CITIES = {
    "NYC": {
        "name": "New York Central Park",
        "lat": 40.7789,
        "lon": -73.9692,
        "series_ticker": "KXHIGHNY",
        "nws_station": "KNYC",
        "timezone": "America/New_York",
    },
    "CHI": {
        "name": "Chicago Midway",
        "lat": 41.7868,
        "lon": -87.7522,
        "series_ticker": "KXHIGHCHI",
        "nws_station": "KMDW",
        "timezone": "America/Chicago",
    },
    "MIA": {
        "name": "Miami International",
        "lat": 25.7959,
        "lon": -80.2870,
        "series_ticker": "KXHIGHMIA",
        "nws_station": "KMIA",
        "timezone": "America/New_York",
    },
    "AUS": {
        "name": "Austin-Bergstrom",
        "lat": 30.1945,
        "lon": -97.6699,
        "series_ticker": "KXHIGHAUS",
        "nws_station": "KAUS",
        "timezone": "America/Chicago",
    },
    "LAX": {
        "name": "Los Angeles",
        "lat": 33.9425,
        "lon": -118.4081,
        "series_ticker": "KXHIGHLAX",
        "nws_station": "KLAX",
        "timezone": "America/Los_Angeles",
    },
    "DEN": {
        "name": "Denver",
        "lat": 39.8561,
        "lon": -104.6737,
        "series_ticker": "KXHIGHDEN",
        "nws_station": "KDEN",
        "timezone": "America/Denver",
    },
    "PHI": {
        "name": "Philadelphia",
        "lat": 39.8744,
        "lon": -75.2424,
        "series_ticker": "KXHIGHPHIL",
        "nws_station": "KPHL",
        "timezone": "America/New_York",
    },
    "ATL": {
        "name": "Atlanta",
        "lat": 33.6407,
        "lon": -84.4277,
        "series_ticker": "KXHIGHTATL",
        "nws_station": "KATL",
        "timezone": "America/New_York",
    },
    "BOS": {
        "name": "Boston",
        "lat": 42.3656,
        "lon": -71.0096,
        "series_ticker": "KXHIGHTBOS",
        "nws_station": "KBOS",
        "timezone": "America/New_York",
    },
    "DAL": {
        "name": "Dallas",
        "lat": 32.8998,
        "lon": -97.0403,
        "series_ticker": "KXHIGHTDAL",
        "nws_station": "KDFW",
        "timezone": "America/Chicago",
    },
    "DC": {
        "name": "Washington DC",
        "lat": 38.8512,
        "lon": -77.0402,
        "series_ticker": "KXHIGHTDC",
        "nws_station": "KDCA",
        "timezone": "America/New_York",
    },
    "HOU": {
        "name": "Houston Hobby",
        "lat": 29.6458,
        "lon": -95.2772,
        "series_ticker": "KXHIGHTHOU",
        "nws_station": "KHOU",
        "timezone": "America/Chicago",
    },
    "LV": {
        "name": "Las Vegas",
        "lat": 36.0840,
        "lon": -115.1537,
        "series_ticker": "KXHIGHTLV",
        "nws_station": "KLAS",
        "timezone": "America/Los_Angeles",
    },
    "MIN": {
        "name": "Minneapolis",
        "lat": 44.8848,
        "lon": -93.2223,
        "series_ticker": "KXHIGHTMIN",
        "nws_station": "KMSP",
        "timezone": "America/Chicago",
    },
    "NOLA": {
        "name": "New Orleans",
        "lat": 29.9934,
        "lon": -90.2580,
        "series_ticker": "KXHIGHTNOLA",
        "nws_station": "KMSY",
        "timezone": "America/Chicago",
    },
    "OKC": {
        "name": "Oklahoma City",
        "lat": 35.3931,
        "lon": -97.6007,
        "series_ticker": "KXHIGHTOKC",
        "nws_station": "KOKC",
        "timezone": "America/Chicago",
    },
    "PHX": {
        "name": "Phoenix",
        "lat": 33.4373,
        "lon": -112.0078,
        "series_ticker": "KXHIGHTPHX",
        "nws_station": "KPHX",
        "timezone": "America/Phoenix",
    },
    "SATX": {
        "name": "San Antonio",
        "lat": 29.5337,
        "lon": -98.4698,
        "series_ticker": "KXHIGHTSATX",
        "nws_station": "KSAT",
        "timezone": "America/Chicago",
    },
    "SEA": {
        "name": "Seattle",
        "lat": 47.4502,
        "lon": -122.3088,
        "series_ticker": "KXHIGHTSEA",
        "nws_station": "KSEA",
        "timezone": "America/Los_Angeles",
    },
    "SFO": {
        "name": "San Francisco",
        "lat": 37.6213,
        "lon": -122.3790,
        "series_ticker": "KXHIGHTSFO",
        "nws_station": "KSFO",
        "timezone": "America/Los_Angeles",
    },
}


NWS_HEADERS = {
    "User-Agent": "KalshiBot/5.0 (trading-bot@example.com)",
    "Accept": "application/geo+json",
}
NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
NWS_LATEST_URL = "https://api.weather.gov/stations/{station}/observations/latest"

_SOURCE_LABELS = {
    "nws_daily": "NWS Daily",
    "nws_hourly": "NWS Hourly",
    "nws_grid_daily": "NWS Grid Daily",
    "nws_grid_hourly": "NWS Grid Hourly",
    "obs_blend": "Observation Blend",
}

_VALID_TIME_RE = re.compile(
    r"^(?P<start>[^/]+)/P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?)?$"
)


def _in_fetch_window():
    """Compatibility helper for legacy tests and callers."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    start = getattr(config, "OPEN_METEO_FETCH_START_ET", 8)
    end = getattr(config, "OPEN_METEO_FETCH_END_ET", 18)
    if getattr(config, "ALLOW_OFF_HOURS_FORECAST_FETCH", False):
        return True
    return start <= now_et.hour < end


def _c_to_f(value_c):
    if value_c is None:
        return None
    return float(value_c) * 9.0 / 5.0 + 32.0


def _parse_valid_time_range(valid_time):
    if not valid_time:
        return None, None
    match = _VALID_TIME_RE.match(valid_time)
    if not match:
        return None, None
    start = datetime.fromisoformat(match.group("start").replace("Z", "+00:00"))
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    duration = timedelta(days=days, hours=hours)
    if duration <= timedelta(0):
        duration = timedelta(hours=1)
    return start, start + duration


def _window_bounds(tz_name, target_date, daytime_only=False):
    tz = ZoneInfo(tz_name)
    target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
    start = datetime.combine(target_day, dt_time(6 if daytime_only else 0, 0), tzinfo=tz)
    end = datetime.combine(target_day + timedelta(days=1), dt_time(0, 0), tzinfo=tz)
    return start, end


def _interval_overlaps_target(start_utc, end_utc, tz_name, target_date, daytime_only=False):
    if start_utc is None or end_utc is None:
        return False
    target_start, target_end = _window_bounds(tz_name, target_date, daytime_only=daytime_only)
    start_local = start_utc.astimezone(target_start.tzinfo)
    end_local = end_utc.astimezone(target_start.tzinfo)
    return start_local < target_end and end_local > target_start


class WeatherEngine:
    """Fetches NWS forecasts and builds a conservative temperature distribution."""

    def __init__(self):
        self._cache = {}
        self._source_cache = {}
        self._points_cache = {}
        self._metar_cache = {}
        self.last_fetch_time = None
        self.last_api_error = None
        self.model_fetch_stats = {}
        self._stats_lock = threading.Lock()

    def get_model_health(self):
        """Return per-source availability stats."""
        result = {}
        with self._stats_lock:
            for source_key, label in _SOURCE_LABELS.items():
                stats = self.model_fetch_stats.get(
                    source_key,
                    {"success": 0, "failure": 0, "last_error": "", "last_success_at": None},
                )
                total = stats["success"] + stats["failure"]
                result[label] = {
                    "success": stats["success"],
                    "failure": stats["failure"],
                    "total": total,
                    "availability_pct": round(100.0 * stats["success"] / total, 1) if total else 0.0,
                    "last_error": stats.get("last_error", ""),
                    "last_success_at": stats.get("last_success_at"),
                }
        return result

    def _record_source_result(self, source_key, success, error=""):
        with self._stats_lock:
            stats = self.model_fetch_stats.setdefault(
                source_key,
                {"success": 0, "failure": 0, "last_error": "", "last_success_at": None},
            )
            if success:
                stats["success"] += 1
                stats["last_success_at"] = datetime.now(timezone.utc).isoformat()
            else:
                stats["failure"] += 1
                if error:
                    stats["last_error"] = error

    def _get_cached_json(self, cache_key, url, ttl, source_key=None, params=None):
        now = datetime.now(timezone.utc)
        cached = self._source_cache.get(cache_key)
        if cached:
            age = (now - cached["fetched_at"]).total_seconds()
            if age < ttl:
                return cached["data"]

        try:
            response = requests.get(url, headers=NWS_HEADERS, params=params, timeout=20)
            if response.status_code != 200:
                err_msg = "%s HTTP %d" % (source_key or cache_key, response.status_code)
                self.last_api_error = err_msg
                if source_key:
                    self._record_source_result(source_key, False, err_msg)
                if cached:
                    return cached["data"]
                return None
            data = response.json()
            self._source_cache[cache_key] = {"data": data, "fetched_at": now}
            return data
        except Exception as exc:
            err_msg = "%s fetch failed: %s" % (source_key or cache_key, exc)
            self.last_api_error = err_msg
            if source_key:
                self._record_source_result(source_key, False, err_msg)
            if cached:
                return cached["data"]
            return None

    def _get_points_metadata(self, city_code, city):
        cached = self._points_cache.get(city_code)
        now = datetime.now(timezone.utc)
        if cached:
            age = (now - cached["fetched_at"]).total_seconds()
            if age < 6 * 3600:
                return cached["data"]

        data = self._get_cached_json(
            cache_key="points_%s" % city_code,
            url=NWS_POINTS_URL.format(lat=city["lat"], lon=city["lon"]),
            ttl=6 * 3600,
        )
        props = (data or {}).get("properties", {})
        if not props:
            return None

        meta = {
            "forecast": props.get("forecast"),
            "forecastHourly": props.get("forecastHourly"),
            "forecastGridData": props.get("forecastGridData"),
        }
        if not all(meta.values()):
            return None

        self._points_cache[city_code] = {"data": meta, "fetched_at": now}
        return meta

    def _extract_period_temperatures(self, payload, target_date, tz_name, daytime_only):
        periods = ((payload or {}).get("properties") or {}).get("periods", [])
        values = []
        for period in periods:
            temp = period.get("temperature")
            if temp is None:
                continue
            try:
                start = datetime.fromisoformat(period.get("startTime", "").replace("Z", "+00:00"))
            except Exception:
                continue
            local_dt = start.astimezone(ZoneInfo(tz_name))
            if local_dt.strftime("%Y-%m-%d") != target_date:
                continue
            if daytime_only:
                if "isDaytime" in period and not period.get("isDaytime", False):
                    continue
                if not 6 <= local_dt.hour <= 23:
                    continue
            values.append(float(temp))
        return values

    def _extract_grid_values(self, payload, field_name, target_date, tz_name, daytime_only=False):
        values = (((payload or {}).get("properties") or {}).get(field_name) or {}).get("values", [])
        extracted = []
        for entry in values:
            value = entry.get("value")
            if value is None:
                continue
            start, end = _parse_valid_time_range(entry.get("validTime", ""))
            if not _interval_overlaps_target(start, end, tz_name, target_date, daytime_only=daytime_only):
                continue
            extracted.append(float(value))
        return extracted

    def _source_uncertainty(self, source_key, day_diff, local_hour, inter_source_spread):
        if day_diff <= 0:
            base = 1.8
        elif day_diff == 1:
            base = 2.6
        else:
            base = 3.4

        if source_key in ("nws_daily", "nws_grid_daily"):
            base += 0.3
        if source_key == "obs_blend":
            base = max(0.9, base - 0.9)
        if day_diff <= 0:
            if local_hour >= 15:
                base -= 0.7
            elif local_hour >= 12:
                base -= 0.3
        base += min(2.0, max(0.0, inter_source_spread) * 0.35)
        return max(0.9, round(base, 2))

    def _build_source_members(self, mean_temp, std_dev, obs_high=None):
        offsets = [-1.8, -1.1, -0.6, -0.2, 0.2, 0.6, 1.1, 1.8]
        weights = [0.04, 0.08, 0.15, 0.23, 0.23, 0.15, 0.08, 0.04]
        members = []
        for offset in offsets:
            temp = round(mean_temp + offset * std_dev, 1)
            if obs_high is not None:
                temp = max(float(obs_high), temp)
            members.append(temp)
        return members, weights

    def _fetch_metar_batch(self):
        cache_key = "metar_batch"
        cached = self._metar_cache.get(cache_key)
        now = datetime.now(timezone.utc)
        ttl = getattr(config, "METAR_CACHE_TTL_SEC", 90)
        if cached:
            age = (now - cached["fetched_at"]).total_seconds()
            if age < ttl:
                return cached["data"]

        stations = [info["nws_station"] for info in CITIES.values() if info.get("nws_station")]
        try:
            resp = requests.get(
                getattr(config, "METAR_API_URL", "https://aviationweather.gov/api/data/metar"),
                params={
                    "ids": ",".join(stations),
                    "format": "json",
                    "hours": getattr(config, "METAR_HOURS_LOOKBACK", 18),
                },
                timeout=getattr(config, "METAR_REQUEST_TIMEOUT", 10),
            )
            if resp.status_code != 200:
                if cached:
                    return cached["data"]
                self.last_api_error = "METAR HTTP %d" % resp.status_code
                return {}

            raw = resp.json()
            if not isinstance(raw, list):
                return {}

            result = {}
            for obs in raw:
                icao = obs.get("icaoId", "")
                temp_c = obs.get("temp")
                obs_time_unix = obs.get("obsTime")
                if not icao or temp_c is None or obs_time_unix is None:
                    continue
                try:
                    temp_f = math.floor(float(temp_c) * 9.0 / 5.0 + 32.0)
                    obs_dt = datetime.fromtimestamp(int(obs_time_unix), tz=timezone.utc)
                except Exception:
                    continue
                result.setdefault(icao, []).append(
                    {
                        "temp_f": temp_f,
                        "obs_time": obs_dt,
                        "cloud_cover": obs.get("cover", ""),
                        "precip": obs.get("precip"),
                    }
                )

            for station_obs in result.values():
                station_obs.sort(key=lambda row: row["obs_time"])

            self._metar_cache[cache_key] = {"data": result, "fetched_at": now}
            return result
        except Exception as exc:
            self.last_api_error = "METAR fetch failed: %s" % exc
            if cached:
                return cached["data"]
            return {}

    def _fetch_observation_context(self, city_code, city, target_date):
        city_now = datetime.now(ZoneInfo(city["timezone"]))
        if target_date != city_now.strftime("%Y-%m-%d"):
            return {}

        station = city.get("nws_station", "")
        metar_data = self._fetch_metar_batch()
        obs_rows = metar_data.get(station, [])
        if not obs_rows:
            return {}

        local_date = city_now.strftime("%Y-%m-%d")
        todays_rows = [
            row
            for row in obs_rows
            if row["obs_time"].astimezone(ZoneInfo(city["timezone"])).strftime("%Y-%m-%d") == local_date
        ]
        if not todays_rows:
            return {}

        latest = todays_rows[-1]
        cover_map = {"CLR": 0, "SKC": 0, "FEW": 20, "SCT": 40, "BKN": 70, "OVC": 95}
        cloud_cover = cover_map.get((latest.get("cloud_cover") or "").upper(), 50)
        precip_mm = 0.0
        if latest.get("precip") is not None:
            try:
                precip_mm = float(latest["precip"])
            except Exception:
                precip_mm = 0.0

        return {
            "todays_high": max(row["temp_f"] for row in todays_rows),
            "current_temp": latest["temp_f"],
            "cloud_cover_pct": cloud_cover,
            "precipitation_mm": precip_mm,
            "observed_at": latest["obs_time"].isoformat(),
        }

    def _estimate_obs_blend(self, source_means, obs_context, city):
        if not obs_context:
            return None

        current_temp = obs_context.get("current_temp")
        todays_high = obs_context.get("todays_high")
        if current_temp is None and todays_high is None:
            return None

        city_now = datetime.now(ZoneInfo(city["timezone"]))
        anchor = max(v for v in (current_temp, todays_high) if v is not None)
        if city_now.hour < 8:
            remaining_warming = 9.0
        elif city_now.hour < 10:
            remaining_warming = 7.0
        elif city_now.hour < 12:
            remaining_warming = 5.5
        elif city_now.hour < 14:
            remaining_warming = 4.0
        elif city_now.hour < 16:
            remaining_warming = 2.5
        elif city_now.hour < 18:
            remaining_warming = 1.5
        else:
            remaining_warming = 0.75

        mean_temp = anchor + remaining_warming
        if source_means:
            source_cap = max(source_means.values()) + max(1.0, (max(source_means.values()) - anchor) * 0.5)
            mean_temp = min(mean_temp, source_cap)
        mean_temp = max(mean_temp, float(todays_high or anchor))

        return {
            "mean": round(mean_temp, 1),
            "uncertainty_f": max(0.9, round(remaining_warming * 0.4, 2)),
        }

    def _fetch_cloud_cover(self, city_code, target_date, grid_payload=None, obs_context=None):
        cache_key = "wx_%s_%s" % (city_code, target_date)
        cached = self._cache.get(cache_key)
        if cached:
            return cached.get("weather_summary")

        city = CITIES.get(city_code)
        if not city:
            return None

        summary = {
            "cloud_cover_pct": None,
            "precipitation_mm": 0.0,
            "wind_speed_max_kmh": 0.0,
        }

        if grid_payload:
            sky_values = self._extract_grid_values(
                grid_payload, "skyCover", target_date, city["timezone"], daytime_only=True
            )
            if sky_values:
                summary["cloud_cover_pct"] = sum(sky_values) / len(sky_values)

            precip_probs = self._extract_grid_values(
                grid_payload,
                "probabilityOfPrecipitation",
                target_date,
                city["timezone"],
                daytime_only=True,
            )
            if precip_probs:
                summary["precipitation_mm"] = max(precip_probs) / 100.0 * 2.0

            wind_values = self._extract_grid_values(
                grid_payload, "windSpeed", target_date, city["timezone"], daytime_only=True
            )
            if wind_values:
                summary["wind_speed_max_kmh"] = max(wind_values)

        if obs_context and target_date == datetime.now(ZoneInfo(city["timezone"])).strftime("%Y-%m-%d"):
            if obs_context.get("cloud_cover_pct") is not None:
                summary["cloud_cover_pct"] = obs_context["cloud_cover_pct"]
            if obs_context.get("precipitation_mm") is not None:
                summary["precipitation_mm"] = max(
                    summary["precipitation_mm"], obs_context["precipitation_mm"]
                )

        if summary["cloud_cover_pct"] is None:
            return None
        return summary

    def _fetch_forecast_sources(self, city_code, city, target_date):
        meta = self._get_points_metadata(city_code, city)
        if not meta:
            return {}, None

        daily_payload = self._get_cached_json(
            cache_key="forecast_daily_%s" % city_code,
            url=meta["forecast"],
            ttl=1800,
            source_key="nws_daily",
        )
        hourly_payload = self._get_cached_json(
            cache_key="forecast_hourly_%s" % city_code,
            url=meta["forecastHourly"],
            ttl=1800,
            source_key="nws_hourly",
        )
        grid_payload = self._get_cached_json(
            cache_key="forecast_grid_%s" % city_code,
            url=meta["forecastGridData"],
            ttl=1800,
            source_key="nws_grid_daily",
        )

        source_means = {}

        daily_vals = self._extract_period_temperatures(
            daily_payload, target_date, city["timezone"], daytime_only=True
        )
        if daily_vals:
            source_means["nws_daily"] = max(daily_vals)
            self._record_source_result("nws_daily", True)
        else:
            self._record_source_result("nws_daily", False, "No NWS daily forecast for %s" % target_date)

        hourly_vals = self._extract_period_temperatures(
            hourly_payload, target_date, city["timezone"], daytime_only=True
        )
        if hourly_vals:
            source_means["nws_hourly"] = max(hourly_vals)
            self._record_source_result("nws_hourly", True)
        else:
            self._record_source_result("nws_hourly", False, "No NWS hourly forecast for %s" % target_date)

        grid_daily_vals = self._extract_grid_values(
            grid_payload, "maxTemperature", target_date, city["timezone"], daytime_only=True
        )
        if grid_daily_vals:
            source_means["nws_grid_daily"] = max(_c_to_f(val) for val in grid_daily_vals)
            self._record_source_result("nws_grid_daily", True)
        else:
            self._record_source_result(
                "nws_grid_daily", False, "No NWS grid max forecast for %s" % target_date
            )

        grid_hourly_vals = self._extract_grid_values(
            grid_payload, "temperature", target_date, city["timezone"], daytime_only=True
        )
        if grid_hourly_vals:
            source_means["nws_grid_hourly"] = max(_c_to_f(val) for val in grid_hourly_vals)
            self._record_source_result("nws_grid_hourly", True)
        else:
            self._record_source_result(
                "nws_grid_hourly", False, "No NWS grid hourly forecast for %s" % target_date
            )

        return source_means, grid_payload

    def get_temperature_distribution(
        self,
        city_code,
        target_date=None,
        model_weights=None,
        model_biases=None,
        city_bias_f=0.0,
    ):
        """
        Build a conservative temperature distribution from NWS + METAR sources.

        The legacy *model_biases* parameter is accepted for compatibility but is
        intentionally ignored because the old Open-Meteo model-specific learning
        no longer applies to this NWS-native stack.
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        if target_date is None:
            target_date = datetime.now(ZoneInfo(city["timezone"])).strftime("%Y-%m-%d")

        weights_key = ""
        if model_weights:
            weights_key = "_" + "_".join("%s:%s" % (k, model_weights[k]) for k in sorted(model_weights))
        bias_key = "_cb%.2f" % float(city_bias_f or 0.0)
        cache_key = "%s_%s%s%s" % (city_code, target_date, weights_key, bias_key)
        ttl = getattr(config, "DISTRIBUTION_CACHE_TTL", 3600)
        cached = self._cache.get(cache_key)
        if cached:
            age = (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds()
            if age < ttl and cached["data"] is not None:
                return cached["data"]

        source_means, grid_payload = self._fetch_forecast_sources(city_code, city, target_date)
        obs_context = self._fetch_observation_context(city_code, city, target_date)
        obs_blend = self._estimate_obs_blend(source_means, obs_context, city)
        if obs_blend:
            source_means["obs_blend"] = obs_blend["mean"]
            self._record_source_result("obs_blend", True)
        else:
            self._record_source_result("obs_blend", False, "Observation blend unavailable")

        if not source_means:
            self._cache[cache_key] = {"data": None, "fetched_at": datetime.now(timezone.utc)}
            return None

        city_now = datetime.now(ZoneInfo(city["timezone"]))
        target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
        day_diff = (target_day - city_now.date()).days
        inter_source_spread = 0.0
        if len(source_means) >= 2:
            inter_source_spread = max(source_means.values()) - min(source_means.values())

        def _weight_for(source_key):
            if model_weights:
                if source_key in model_weights:
                    return model_weights[source_key]
                vals = [value for value in model_weights.values() if isinstance(value, (int, float))]
                if vals:
                    return sum(vals) / len(vals)
            return 1.0

        cloud_data = self._fetch_cloud_cover(
            city_code, target_date, grid_payload=grid_payload, obs_context=obs_context
        )
        cloud_adj = 0.0
        precip_adj = 0.0
        wind_adj = 0.0
        total_weather_adj = 0.0
        if cloud_data:
            if cloud_data["cloud_cover_pct"] > config.CLOUD_COVER_THRESHOLD_PCT:
                cloud_adj = config.CLOUD_COVER_TEMP_BIAS_F
            if cloud_data["precipitation_mm"] > config.PRECIP_THRESHOLD_MM:
                precip_adj = config.PRECIP_TEMP_BIAS_F
            if cloud_data["wind_speed_max_kmh"] > getattr(config, "WIND_SPEED_THRESHOLD_KMH", 32):
                wind_adj = getattr(config, "WIND_SPEED_TEMP_BIAS_F", -1.0)
            total_weather_adj = max(
                getattr(config, "WEATHER_BIAS_CAP_F", -2.0),
                cloud_adj + precip_adj + wind_adj,
            )

        obs_high = obs_context.get("todays_high")
        all_highs = []
        all_weights = []
        model_means = {}
        model_stds = {}
        raw_means = []

        for source_key, raw_mean in source_means.items():
            raw_means.append(float(raw_mean))
            adjusted_mean = float(raw_mean) + float(city_bias_f or 0.0) + total_weather_adj
            if day_diff <= 0 and obs_high is not None:
                adjusted_mean = max(adjusted_mean, float(obs_high))
            uncertainty = self._source_uncertainty(
                source_key, day_diff, city_now.hour, inter_source_spread
            )
            if source_key == "obs_blend" and obs_blend:
                uncertainty = max(0.9, float(obs_blend.get("uncertainty_f", uncertainty)))

            members, member_weights = self._build_source_members(
                adjusted_mean,
                uncertainty,
                obs_high=float(obs_high) if day_diff <= 0 and obs_high is not None else None,
            )
            source_weight = _weight_for(source_key)
            all_highs.extend(members)
            all_weights.extend([weight * source_weight for weight in member_weights])
            model_means[_SOURCE_LABELS.get(source_key, source_key)] = round(adjusted_mean, 1)
            model_stds[_SOURCE_LABELS.get(source_key, source_key)] = round(uncertainty, 2)

        result = self._build_distribution(
            city_code,
            target_date,
            all_highs,
            [_SOURCE_LABELS.get(key, key) for key in source_means.keys()],
            all_weights,
        )
        if result is None:
            self._cache[cache_key] = {"data": None, "fetched_at": datetime.now(timezone.utc)}
            return None

        if raw_means:
            result["raw_forecast_mean"] = round(sum(raw_means) / len(raw_means), 1)
        result["cloud_cover_adj_f"] = cloud_adj
        result["precip_adj_f"] = precip_adj
        result["wind_adj_f"] = wind_adj
        result["model_means"] = model_means
        result["model_stds"] = model_stds
        if len(model_means) >= 2:
            means_list = list(model_means.values())
            result["model_spread"] = round(max(means_list) - min(means_list), 1)
        else:
            result["model_spread"] = 0.0
        if obs_context:
            result["observation_context"] = {
                "todays_high": obs_context.get("todays_high"),
                "current_temp": obs_context.get("current_temp"),
                "observed_at": obs_context.get("observed_at"),
            }

        self._cache[cache_key] = {
            "data": result,
            "fetched_at": datetime.now(timezone.utc),
            "weather_summary": cloud_data,
        }
        self.last_fetch_time = datetime.now(timezone.utc)
        return result

    def get_nws_confirmation(self, city_code, target_date=None):
        """Return the best available NWS-native forecast anchor."""
        city = CITIES.get(city_code)
        if not city:
            return None

        if target_date is None:
            target_date = datetime.now(ZoneInfo(city["timezone"])).strftime("%Y-%m-%d")

        source_means, _grid_payload = self._fetch_forecast_sources(city_code, city, target_date)
        if not source_means:
            return None

        for source_key in ("nws_daily", "nws_hourly", "nws_grid_daily", "nws_grid_hourly"):
            if source_key in source_means:
                return {
                    "high_temp": round(source_means[source_key]),
                    "source": _SOURCE_LABELS.get(source_key, source_key),
                }
        return None

    def _build_distribution(self, city_code, target_date, all_highs, sources_used, weights=None):
        """Build weighted summary statistics and bucket probabilities."""
        n = len(all_highs)
        if n == 0:
            return None
        if weights is None:
            weights = [1.0] * n

        total_weight = sum(weights)
        if total_weight <= 0:
            return None

        sorted_pairs = sorted(zip(all_highs, weights), key=lambda item: item[0])
        sorted_highs = [temp for temp, _weight in sorted_pairs]
        sorted_weights = [weight for _temp, weight in sorted_pairs]

        mean_temp = sum(temp * weight for temp, weight in zip(all_highs, weights)) / total_weight
        median_temp = sorted_highs[n // 2]
        min_temp = sorted_highs[0]
        max_temp = sorted_highs[-1]
        spread = max_temp - min_temp
        variance = sum(weight * (temp - mean_temp) ** 2 for temp, weight in zip(all_highs, weights)) / total_weight
        std_dev = math.sqrt(variance)

        bucket_counts = defaultdict(float)
        fine_bucket_counts = defaultdict(float)
        for index, temp in enumerate(all_highs):
            bucket_floor = int(temp // 5) * 5
            bucket_counts["%d-%d" % (bucket_floor, bucket_floor + 4)] += weights[index]
            fine_floor = int(temp // 2) * 2
            fine_bucket_counts["%d-%d" % (fine_floor, fine_floor + 1)] += weights[index]

        bucket_probs = {key: value / total_weight for key, value in sorted(bucket_counts.items())}
        fine_bucket_probs = {
            key: value / total_weight for key, value in sorted(fine_bucket_counts.items())
        }
        confidence = max(0.35, min(0.95, 1.0 - (std_dev / 10.0)))

        return {
            "city": city_code,
            "target_date": target_date,
            "forecasted_high_mean": round(mean_temp, 1),
            "forecasted_high_median": round(median_temp, 1),
            "forecasted_high_min": round(min_temp, 1),
            "forecasted_high_max": round(max_temp, 1),
            "spread": round(spread, 1),
            "std_dev": round(std_dev, 1),
            "total_members": n,
            "bucket_probs_5f": bucket_probs,
            "bucket_probs_2f": fine_bucket_probs,
            "raw_highs": sorted_highs,
            "raw_weights": sorted_weights,
            "sources_used": sources_used,
            "confidence": round(confidence, 3),
        }

    # ---------------------------------------------------------
    # UTILITY: Match a market title to a temperature bucket
    # ---------------------------------------------------------

    def parse_market_bucket(self, market):
        """
        Parse a Kalshi weather market into city + bucket info.
        """
        ticker = market.get("ticker", "").upper()
        title = market.get("title", "")
        subtitle = market.get("subtitle", "")
        event_ticker = market.get("event_ticker", "").upper()

        city_code = None
        for code, info in CITIES.items():
            if info["series_ticker"].upper() in ticker or info["series_ticker"].upper() in event_ticker:
                city_code = code
                break

        if not city_code:
            return None

        temp_low = None
        temp_high = None

        range_match = re.search(r"(\d+)\s*(?:F?\s*)?(?:to|-)\s*(\d+)", title + " " + subtitle)
        if range_match:
            temp_low = int(range_match.group(1))
            temp_high = int(range_match.group(2))
        else:
            above_match = re.search(
                r"(?:above|over|higher than|at least|>=|>)\s*(\d+)",
                title + " " + subtitle,
                re.I,
            )
            if not above_match:
                above_match = re.search(r"(\d+)\s*or\s*above", title + " " + subtitle, re.I)
            below_match = re.search(
                r"(?:below|under|lower than|less than|<=|<)\s*(\d+)",
                title + " " + subtitle,
                re.I,
            )
            if not below_match:
                below_match = re.search(r"(\d+)\s*or\s*below", title + " " + subtitle, re.I)

            if above_match:
                threshold = int(above_match.group(1))
                matched_text = (title + " " + subtitle)[above_match.start() : above_match.end()]
                if re.search(r"\d+\s*or\s*above", matched_text, re.I):
                    temp_low = threshold
                else:
                    temp_low = threshold + 1
                temp_high = 200
            elif below_match:
                threshold = int(below_match.group(1))
                matched_text = (title + " " + subtitle)[below_match.start() : below_match.end()]
                if re.search(r"\d+\s*or\s*below", matched_text, re.I):
                    temp_high = threshold
                else:
                    temp_high = threshold - 1
                temp_low = -100

        if temp_low is None:
            bucket_match = re.search(r"-B(\d+\.?\d*)", ticker)
            thresh_match = re.search(r"-T(\d+\.?\d*)", ticker)
            if bucket_match:
                midpoint = float(bucket_match.group(1))
                temp_low = int(midpoint - 0.5)
                temp_high = int(midpoint + 0.5)
            elif thresh_match:
                return None
            else:
                return None

        date_match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
        if date_match:
            year = 2000 + int(date_match.group(1))
            month_str = date_match.group(2)
            day = int(date_match.group(3))
            months = {
                "JAN": 1,
                "FEB": 2,
                "MAR": 3,
                "APR": 4,
                "MAY": 5,
                "JUN": 6,
                "JUL": 7,
                "AUG": 8,
                "SEP": 9,
                "OCT": 10,
                "NOV": 11,
                "DEC": 12,
            }
            month = months.get(month_str, 0)
            if month > 0:
                target_date = "%04d-%02d-%02d" % (year, month, day)
            else:
                target_date = None
        else:
            target_date = None

        return {
            "city_code": city_code,
            "temp_low": temp_low,
            "temp_high": temp_high,
            "target_date": target_date,
        }

    def calculate_bucket_probability(self, distribution, temp_low, temp_high):
        """Calculate the probability that the high falls in a given range."""
        if not distribution or not distribution.get("raw_highs"):
            return None

        highs = distribution["raw_highs"]
        weights = distribution.get("raw_weights")

        if weights and len(weights) == len(highs):
            total_weight = sum(weights)
            if total_weight == 0:
                return None
            in_range_weight = sum(
                weight for temp, weight in zip(highs, weights) if temp_low <= temp <= temp_high
            )
            return in_range_weight / total_weight

        n = len(highs)
        if n == 0:
            return None
        in_range = sum(1 for temp in highs if temp_low <= temp <= temp_high)
        return in_range / n

    def update_distribution_with_observation(self, distribution, obs_high, local_hour):
        """Shift a distribution toward observed temperatures later in the day."""
        if distribution is None or obs_high is None or local_hour < 10:
            return distribution

        forecast_mean = distribution.get("forecasted_high_mean")
        if forecast_mean is None:
            return distribution

        obs_weight = min(0.6, max(0.0, (local_hour - 9) / 15.0))
        shift = (obs_high - forecast_mean) * obs_weight

        raw_highs = distribution.get("raw_highs")
        raw_weights = distribution.get("raw_weights")

        if raw_highs and len(raw_highs) > 1:
            if obs_high > forecast_mean:
                adjusted = []
                adj_weights = []
                for index, temp in enumerate(raw_highs):
                    if temp >= obs_high:
                        adjusted.append(temp)
                    else:
                        adjusted.append(max(obs_high, temp + shift))
                    if raw_weights and index < len(raw_weights):
                        adj_weights.append(raw_weights[index])
                    else:
                        adj_weights.append(1.0)
            else:
                adjusted = [temp + shift for temp in raw_highs]
                adj_weights = raw_weights if raw_weights else [1.0] * len(adjusted)

            total_weight = sum(adj_weights)
            if total_weight > 0:
                new_mean = sum(temp * weight for temp, weight in zip(adjusted, adj_weights)) / total_weight
                variance = (
                    sum(weight * (temp - new_mean) ** 2 for temp, weight in zip(adjusted, adj_weights))
                    / total_weight
                )
                new_std = math.sqrt(variance)
                distribution["forecasted_high_mean"] = round(new_mean, 1)
                distribution["std_dev"] = round(new_std, 1)
                sorted_pairs = sorted(zip(adjusted, adj_weights))
                distribution["raw_highs"] = [temp for temp, _weight in sorted_pairs]
                distribution["raw_weights"] = [weight for _temp, weight in sorted_pairs]

                bucket_counts = defaultdict(float)
                fine_counts = defaultdict(float)
                for temp, weight in sorted_pairs:
                    bucket_floor = int(temp // 5) * 5
                    bucket_counts["%d-%d" % (bucket_floor, bucket_floor + 4)] += weight
                    fine_floor = int(temp // 2) * 2
                    fine_counts["%d-%d" % (fine_floor, fine_floor + 1)] += weight
                distribution["bucket_probs_5f"] = {
                    key: value / total_weight for key, value in sorted(bucket_counts.items())
                }
                distribution["bucket_probs_2f"] = {
                    key: value / total_weight for key, value in sorted(fine_counts.items())
                }
        else:
            distribution["forecasted_high_mean"] = round(forecast_mean + shift, 1)

        distribution["obs_adjusted"] = True
        distribution["obs_shift_f"] = round(shift, 2)
        return distribution

    def print_status(self):
        """Print cached forecast distributions."""
        if not self._cache:
            print("  [WEATHER] No forecasts cached yet")
            return

        for key, cached in self._cache.items():
            data = cached.get("data")
            if not isinstance(data, dict) or "forecasted_high_mean" not in data:
                continue
            age_min = (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds() / 60.0
            print(
                "  [WEATHER] %s %s: mean=%sF, spread=%sF, %d members, cached %.0fmin ago"
                % (
                    data["city"],
                    data["target_date"],
                    data["forecasted_high_mean"],
                    data["std_dev"],
                    data["total_members"],
                    age_min,
                )
            )
