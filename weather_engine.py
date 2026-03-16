"""
WEATHER FORECAST ENGINE v4.0
================================
Fetches ensemble forecasts from multiple weather models via Open-Meteo API.
Builds probability distributions across temperature buckets.
Compares against Kalshi market prices to find mispricings.

DATA SOURCES (all free, no API key needed):
  - GFS Ensemble:   31 members (NOAA, US)
  - ECMWF IFS:      51 members (European Centre)
  - ICON-EPS:       40 members (DWD, Germany)
  - GEM Ensemble:   21 members (Canada)
  ─────────────────────────────
  TOTAL:           143 ensemble members

Each member is a slightly different simulation of the atmosphere.
By counting how many members predict each temperature bucket,
we build a probability distribution that's far more robust than
any single forecast.

SETTLEMENT SOURCE:
  Kalshi weather markets settle on NWS Daily Climate Report.
  NWS stations: KNYC (Central Park), KMDW (Chicago Midway),
                KMIA (Miami Intl), KAUS (Austin-Bergstrom),
                KLAX (Los Angeles), KDEN (Denver), KPHL (Philadelphia),
                KATL (Atlanta), KBOS (Boston), KDFW (Dallas-Fort Worth),
                KDCA (Washington DC), KHOU (Houston Hobby), KLAS (Las Vegas),
                KMSP (Minneapolis), KMSY (New Orleans), KOKC (Oklahoma City),
                KPHX (Phoenix), KSAT (San Antonio), KSEA (Seattle),
                KSFO (San Francisco)
"""

import requests
import math
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import json
import time
import config

# ─────────────────────────────────────────────────────────
# CITY CONFIGURATION
# ─────────────────────────────────────────────────────────
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

# Open-Meteo API endpoints — use customer API if key is set (removes rate limits)
_OM_KEY = getattr(config, "OPEN_METEO_API_KEY", "") if hasattr(config, "OPEN_METEO_API_KEY") else ""
if _OM_KEY:
    ENSEMBLE_API = "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
    NWS_FORECAST_API = "https://customer-api.open-meteo.com/v1/gfs"
    FORECAST_API = "https://customer-api.open-meteo.com/v1/forecast"
    print("  [WEATHER] Using Open-Meteo customer API (paid key)")
else:
    ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
    NWS_FORECAST_API = "https://api.open-meteo.com/v1/gfs"
    FORECAST_API = "https://api.open-meteo.com/v1/forecast"


def _in_fetch_window():
    """Check if current ET hour is within the Open-Meteo fetch window.
    Outside this window, skip API calls and use stale cache data instead.
    This keeps us within the free tier (10K requests/day).
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    start = getattr(config, "OPEN_METEO_FETCH_START_ET", 8)
    end = getattr(config, "OPEN_METEO_FETCH_END_ET", 18)
    # If key is set, always allow fetching (paid tier has no limit)
    if _OM_KEY:
        return True
    return start <= now_et.hour < end


class WeatherEngine:
    """
    Fetches ensemble forecasts and builds temperature probability distributions.
    This is the core edge generator for weather trading.
    """

    def __init__(self):
        self._cache = {}
        self._nws_cache = {}
        self._cloud_cache = {}  # Independent cloud cover cache (30 min TTL)
        self.last_fetch_time = None
        self.last_api_error = None  # Track last Open-Meteo error for diagnostics

    # ═══════════════════════════════════════════════════════
    # MAIN ENTRY: Get probability distribution for a city
    # ═══════════════════════════════════════════════════════

    def get_temperature_distribution(self, city_code, target_date=None, model_weights=None,
                                     model_biases=None, city_bias_f=0.0):
        """
        Fetch all ensemble members and build a probability distribution
        of high temperatures for the given city and date.

        Args:
            model_weights: Optional dict of per-model accuracy weights from
                quant_analytics.get_model_weights(). E.g. {"gfs_ensemble": 0.8, "ecmwf_ifs": 1.5}.
                If None, all models weighted equally (backward compatible).
            model_biases: Optional dict of per-model mean signed error from
                quant_analytics.get_model_bias(). E.g. {"gfs_ensemble": -2.5}.
                Negative = underpredicts. Correction = -bias is applied per family.

        Returns dict with forecasted_high_mean, bucket_probs, raw_highs, etc.
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Check cache — models update every 6 hours, 15-min cache is appropriate.
        weights_key = ""
        if model_weights:
            weights_key = "_" + "_".join("%s:%s" % (k, model_weights[k]) for k in sorted(model_weights))
        bias_key = "_cb%.2f" % float(city_bias_f or 0.0)
        cache_key = f"{city_code}_{target_date}{weights_key}{bias_key}"
        pos_ttl = getattr(config, "DISTRIBUTION_CACHE_TTL", 900)
        in_window = _in_fetch_window()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            ttl = 60 if cached["data"] is None else pos_ttl
            # Outside fetch window: return ANY cached data (even stale)
            if not in_window or age < ttl:
                return cached["data"]

        # Outside fetch window with no cache: skip API calls entirely
        if not in_window:
            return None

        # Fetch from all ensemble sources
        sources_used = []
        model_family_highs = {}  # Track per-model family for disagreement detection

        # Map from display name to quant_analytics key
        _MODEL_QUANT_KEYS = {
            "GFS": "gfs_ensemble",
            "ECMWF": "ecmwf_ifs",
            "ICON": "icon_eps",
            "GEM": "gem_ensemble",
        }

        # Source 1: GFS Ensemble (30 members)
        gfs_highs = self._fetch_ensemble(city, target_date, "gfs_seamless")
        if gfs_highs:
            sources_used.append("gfs_ensemble")
            model_family_highs["GFS"] = gfs_highs

        # Source 2: ECMWF IFS Ensemble (50 members)
        ecmwf_highs = self._fetch_ensemble(city, target_date, "ecmwf_ifs025")
        if ecmwf_highs:
            sources_used.append("ecmwf_ifs")
            model_family_highs["ECMWF"] = ecmwf_highs

        # Source 3: ICON-EPS (39 members)
        icon_highs = self._fetch_ensemble(city, target_date, "icon_seamless_eps")
        if icon_highs:
            sources_used.append("icon_eps")
            model_family_highs["ICON"] = icon_highs

        # Source 4: GEM Ensemble (20 members)
        gem_highs = self._fetch_ensemble(city, target_date, "gem_global")
        if gem_highs:
            sources_used.append("gem_ensemble")
            model_family_highs["GEM"] = gem_highs

        if not model_family_highs:
            print(f"  [WEATHER] WARN: No ensemble data for {city_code} on {target_date}")
            # Negative cache: avoid re-hitting failed API for 60s
            self._cache[cache_key] = {
                "data": None,
                "fetched_at": datetime.now(),
            }
            return None

        # ─── PER-MODEL BIAS CORRECTION ───
        # Apply learned per-model bias (from settlement accuracy) or per-city
        # winter defaults. Replaces the old flat WINTER_WARM_CITY_BIAS_F.
        # Correction = -bias (negative bias = underpredicts → shift UP)
        target_month = target_date.month if hasattr(target_date, 'month') else None
        if target_month is None:
            try:
                from datetime import datetime as _dt
                target_month = _dt.strptime(str(target_date), "%Y-%m-%d").month
            except Exception:
                target_month = None

        is_winter = target_month in (12, 1, 2, 3) if target_month else False
        # Hardcoded warm city bias defaults (replaces config dependency)
        _WARM_CITY_BIAS = {
            "DEN": 4.0, "HOU": 3.0, "NOLA": 3.0, "MIA": 3.0,
            "ATL": 3.0, "AUS": 3.0, "DAL": 3.0, "SATX": 3.0, "OKC": 3.0,
            "PHX": 2.0, "LV": 2.0,
            "SEA": 2.0, "SFO": 1.5,
        }

        all_highs = []
        all_weights = []
        raw_uncorrected_highs = []  # Track original highs before bias correction
        corrected_family_highs = {}  # Track bias-corrected highs for model_means

        def _weight_for(model_key):
            """Look up accuracy weight for a model, default 1.0."""
            if model_weights:
                return model_weights.get(model_key, 1.0)
            return 1.0

        for family_name, highs in model_family_highs.items():
            quant_key = _MODEL_QUANT_KEYS.get(family_name, family_name.lower())
            w = _weight_for(quant_key)

            # Determine bias correction for this model family
            correction = 0.0
            if model_biases and quant_key in model_biases:
                # Learned bias: negative = underpredicts → correction is positive
                learned_bias = model_biases[quant_key]
                correction = -learned_bias
                print(f"  [WEATHER] {family_name}: learned bias {learned_bias:+.1f}F -> correction {correction:+.1f}F")
            elif is_winter and city_code in _WARM_CITY_BIAS:
                # Fall back to per-city default (no learned data yet)
                correction = _WARM_CITY_BIAS.get(city_code, 0.0)
                if correction > 0:
                    print(f"  [WEATHER] {family_name}: default winter bias +{correction:.1f}°F for {city_code}")

            # Apply correction and build all_highs/all_weights
            total_correction = correction + float(city_bias_f or 0.0)
            corrected_highs = [t + total_correction for t in highs] if total_correction != 0 else highs
            corrected_family_highs[family_name] = corrected_highs
            all_highs.extend(corrected_highs)
            raw_uncorrected_highs.extend(highs)
            all_weights.extend([w] * len(corrected_highs))

        # ─── CLOUD COVER / PRECIPITATION BIAS ───
        # Applied BEFORE building distribution so bucket probabilities reflect the bias
        cloud_data = self._fetch_cloud_cover(city_code, target_date)
        cloud_adj = 0.0
        precip_adj = 0.0
        if cloud_data:
            if cloud_data["cloud_cover_pct"] > config.CLOUD_COVER_THRESHOLD_PCT:
                cloud_adj = config.CLOUD_COVER_TEMP_BIAS_F
                print(f"  [WEATHER] {city_code}: cloud cover {cloud_data['cloud_cover_pct']:.0f}% > {config.CLOUD_COVER_THRESHOLD_PCT}% -> bias {cloud_adj:+.1f}F")
            if cloud_data["precipitation_mm"] > config.PRECIP_THRESHOLD_MM:
                precip_adj = config.PRECIP_TEMP_BIAS_F
                print(f"  [WEATHER] {city_code}: precip {cloud_data['precipitation_mm']:.1f}mm > {config.PRECIP_THRESHOLD_MM}mm -> bias {precip_adj:+.1f}F")
            total_weather_adj = cloud_adj + precip_adj
            if total_weather_adj != 0:
                all_highs = [t + total_weather_adj for t in all_highs]

        # Build the distribution with accuracy-based weights
        result = self._build_distribution(city_code, target_date, all_highs, sources_used, all_weights)

        # Store raw (pre-bias) mean for separation checks — bias shifts the mean
        # toward buckets and can block valid NO trades (especially winter warm cities)
        if raw_uncorrected_highs:
            result["raw_forecast_mean"] = round(sum(raw_uncorrected_highs) / len(raw_uncorrected_highs), 1)

        result["cloud_cover_adj_f"] = cloud_adj
        result["precip_adj_f"] = precip_adj

        # Add per-model family means for disagreement detection
        # Uses bias-corrected highs so model_spread reflects actual predictions
        model_means = {}
        for model_name, highs in corrected_family_highs.items():
            if highs:
                model_means[model_name] = round(sum(highs) / len(highs), 1)
        # Per-model standard deviations for CRPS-based weighting
        model_stds = {}
        for model_name, highs in corrected_family_highs.items():
            if highs and len(highs) >= 2:
                m = sum(highs) / len(highs)
                model_stds[model_name] = round(
                    (sum((t - m) ** 2 for t in highs) / len(highs)) ** 0.5, 2)
        result["model_stds"] = model_stds

        result["model_means"] = model_means
        if len(model_means) >= 2:
            means_list = list(model_means.values())
            result["model_spread"] = round(max(means_list) - min(means_list), 1)
        else:
            result["model_spread"] = 0.0

        # Cache it
        self._cache[cache_key] = {
            "data": result,
            "fetched_at": datetime.now(),
        }
        self.last_fetch_time = datetime.now()

        return result

    # ═══════════════════════════════════════════════════════
    # CONFIRMATION: Get NWS deterministic forecast
    # ═══════════════════════════════════════════════════════

    def get_nws_confirmation(self, city_code, target_date=None):
        """
        Get the deterministic NWS/GFS high-res forecast.
        Used as one of the 'second opinion' confirmation sources.

        Returns {"high_temp": 38, "source": "NWS/GFS HRRR"}
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        # Default target_date BEFORE building cache key to avoid "None" in key
        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        cache_key = f"nws_{city_code}_{target_date}"
        in_window = _in_fetch_window()
        if cache_key in self._nws_cache:
            cached = self._nws_cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if not in_window or age < 1800:
                return cached["data"]

        # Outside fetch window with no cache: skip
        if not in_window:
            return None

        try:
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": city["timezone"],
                "forecast_days": 3,
            }
            if _OM_KEY:
                params["apikey"] = _OM_KEY
            response = requests.get(NWS_FORECAST_API, params=params, timeout=15)
            if response.status_code != 200:
                return None

            data = response.json()
            dates = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])

            for i, d in enumerate(dates):
                if d == target_date and i < len(temps) and temps[i] is not None:
                    result = {
                        "high_temp": round(temps[i]),
                        "source": "NWS/GFS HRRR",
                    }
                    self._nws_cache[cache_key] = {
                        "data": result,
                        "fetched_at": datetime.now(),
                    }
                    return result

        except Exception as e:
            print(f"  [WEATHER] NWS confirmation fetch error: {e}")

        return None

    # ═══════════════════════════════════════════════════════
    # INTERNAL: Fetch ensemble data from Open-Meteo
    # ═══════════════════════════════════════════════════════

    def _fetch_ensemble(self, city, target_date, model):
        """
        Fetch hourly temperature_2m from a specific ensemble model.
        Extract the daily high for target_date from each member.
        Returns list of high temperatures (one per ensemble member).
        """
        # Per-model cache: avoids re-fetching same model for same city/date
        ens_key = f"ens_{city.get('name','')}_{target_date}_{model}"
        pos_ttl = getattr(config, "ENSEMBLE_CACHE_TTL", 900)
        in_window = _in_fetch_window()
        if ens_key in self._cache:
            cached = self._cache[ens_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            ttl = 60 if cached["data"] is None else pos_ttl
            if not in_window or age < ttl:
                return cached["data"]

        # Outside fetch window with no cache: skip API call
        if not in_window:
            return None

        result = self._fetch_ensemble_raw(city, target_date, model)
        self._cache[ens_key] = {"data": result, "fetched_at": datetime.now()}
        return result

    def _fetch_ensemble_raw(self, city, target_date, model):
        """Raw ensemble fetch (no caching)."""
        try:
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "hourly": "temperature_2m",
                "models": model,
                "temperature_unit": "fahrenheit",
                "timezone": city["timezone"],
                "forecast_days": 3,
            }
            if _OM_KEY:
                params["apikey"] = _OM_KEY

            response = requests.get(ENSEMBLE_API, params=params, timeout=20)

            if response.status_code != 200:
                err_msg = "%s HTTP %d for %s" % (model, response.status_code, city.get("name", "?"))
                try:
                    err_msg += " body=%s" % response.text[:200]
                except Exception:
                    pass
                print("  [WEATHER] %s" % err_msg)
                self.last_api_error = err_msg
                return None

            data = response.json()
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])

            if not times:
                return None

            # Find indices for the target date (daytime hours 6 AM - 11 PM)
            target_indices = []
            for i, t in enumerate(times):
                if t.startswith(target_date):
                    # Extract hour
                    hour = int(t[11:13])
                    if 6 <= hour <= 23:  # Daytime hours only
                        target_indices.append(i)

            if not target_indices:
                return None

            # Extract high temp for each ensemble member
            member_highs = []

            # Ensemble data comes as temperature_2m_member01, member02, etc.
            # OR as a single key with array of arrays
            member_keys = [k for k in hourly.keys() if k.startswith("temperature_2m")]
            member_keys = [k for k in member_keys if k != "time"]

            for key in member_keys:
                values = hourly[key]
                if not values:
                    continue

                # Get max temp during daytime hours for this member
                daytime_temps = []
                for idx in target_indices:
                    if idx < len(values) and values[idx] is not None:
                        daytime_temps.append(values[idx])

                if daytime_temps:
                    member_highs.append(round(max(daytime_temps), 1))

            # Filter out NaN/Inf values from API data
            import math
            member_highs = [h for h in member_highs
                           if h is not None and not math.isnan(h) and not math.isinf(h)]
            return member_highs if member_highs else None

        except Exception as e:
            err_msg = "%s fetch failed for %s: %s" % (model, city.get("name", "?"), e)
            print("  [WEATHER] %s" % err_msg)
            self.last_api_error = err_msg
            return None

    def _fetch_cloud_cover(self, city_code, target_date):
        """
        Fetch daily cloud cover and precipitation from Open-Meteo.
        Used to apply temperature bias (clouds/rain suppress highs).

        Returns {"cloud_cover_pct": float, "precipitation_mm": float} or None.
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        # Independent 30-min cache (cloud cover is daily data, changes slowly)
        cc_key = f"cc_{city_code}_{target_date}"
        cc_ttl = getattr(config, "CLOUD_COVER_CACHE_TTL", 1800)
        in_window = _in_fetch_window()
        if cc_key in self._cloud_cache:
            cached = self._cloud_cache[cc_key]
            age = (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds()
            if not in_window or age < cc_ttl:
                return cached["data"]

        # Outside fetch window with no cache: skip API call
        if not in_window:
            return None

        try:
            _cc_url = FORECAST_API
            _cc_params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "cloud_cover_mean,precipitation_sum",
                "timezone": "auto",
                "start_date": target_date,
                "end_date": target_date,
            }
            if _OM_KEY:
                _cc_params["apikey"] = _OM_KEY
            resp = requests.get(_cc_url, params=_cc_params, timeout=10)
            if resp.status_code != 200:
                return None

            data = resp.json()
            daily = data.get("daily", {})
            cloud_vals = daily.get("cloud_cover_mean", [])
            precip_vals = daily.get("precipitation_sum", [])

            cloud_cover = cloud_vals[0] if cloud_vals and cloud_vals[0] is not None else 0.0
            precip = precip_vals[0] if precip_vals and precip_vals[0] is not None else 0.0

            import math
            if not isinstance(cloud_cover, (int, float)) or math.isnan(cloud_cover) or math.isinf(cloud_cover):
                cloud_cover = 0.0
            if not isinstance(precip, (int, float)) or math.isnan(precip) or math.isinf(precip):
                precip = 0.0

            result = {"cloud_cover_pct": float(cloud_cover), "precipitation_mm": float(precip)}
            self._cloud_cache[cc_key] = {"data": result, "fetched_at": datetime.now(timezone.utc)}
            return result

        except Exception as e:
            print(f"  [WEATHER] Cloud cover fetch error for {city_code}: {e}")
            return None

    # ═══════════════════════════════════════════════════════
    # INTERNAL: Build probability distribution from raw highs
    # ═══════════════════════════════════════════════════════

    def _build_distribution(self, city_code, target_date, all_highs, sources_used, weights=None):
        """
        Take all ensemble member high temps and build:
        1. Statistics (weighted mean, median, min, max, spread)
        2. Bucket probabilities (matching Kalshi's 5°F brackets, accuracy-weighted)
        3. Confidence score

        weights: Optional parallel list of per-member accuracy weights.
                 If None, all members weighted equally (backward compatible).
        """
        n = len(all_highs)
        if n == 0:
            return None
        if weights is None:
            weights = [1.0] * n

        total_weight = sum(weights)
        if total_weight <= 0:
            return None

        # Sort highs with their weights for median/min/max
        sorted_pairs = sorted(zip(all_highs, weights), key=lambda x: x[0])
        sorted_highs = [t for t, w in sorted_pairs]
        sorted_weights = [w for t, w in sorted_pairs]

        # Weighted mean
        mean_temp = sum(t * w for t, w in zip(all_highs, weights)) / total_weight
        # Median uses unweighted position (robust to outliers)
        median_temp = sorted_highs[n // 2]
        min_temp = sorted_highs[0]
        max_temp = sorted_highs[-1]
        spread = max_temp - min_temp

        # Weighted standard deviation
        variance = sum(w * (t - mean_temp) ** 2 for t, w in zip(all_highs, weights)) / total_weight
        std_dev = math.sqrt(variance)

        # Build 5°F bucket probabilities with accuracy-based weighting
        # More accurate models contribute more to bucket probabilities
        bucket_counts = defaultdict(float)
        for i, temp in enumerate(all_highs):
            bucket_floor = int(temp // 5) * 5
            bucket_label = f"{bucket_floor}-{bucket_floor + 4}"
            bucket_counts[bucket_label] += weights[i]

        # Also build 2°F buckets for finer resolution
        fine_bucket_counts = defaultdict(float)
        for i, temp in enumerate(all_highs):
            bucket_floor = int(temp // 2) * 2
            fine_bucket_label = f"{bucket_floor}-{bucket_floor + 1}"
            fine_bucket_counts[fine_bucket_label] += weights[i]

        # Convert to weighted probabilities
        bucket_probs = {k: v / total_weight for k, v in sorted(bucket_counts.items())}
        fine_bucket_probs = {k: v / total_weight for k, v in sorted(fine_bucket_counts.items())}

        # Confidence: tighter spread = higher confidence
        confidence = max(0.3, min(0.95, 1.0 - (std_dev / 10.0)))

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

    # ═══════════════════════════════════════════════════════
    # UTILITY: Match a market title to a temperature bucket
    # ═══════════════════════════════════════════════════════

    def parse_market_bucket(self, market):
        """
        Parse a Kalshi weather market to extract:
        - city code
        - temperature range (low, high)
        - target date

        Returns dict or None if not a weather market.
        """
        ticker = market.get("ticker", "").upper()
        title = market.get("title", "")
        subtitle = market.get("subtitle", "")
        event_ticker = market.get("event_ticker", "").upper()

        # Identify city from ticker/event
        city_code = None
        for code, info in CITIES.items():
            if info["series_ticker"].upper() in ticker or info["series_ticker"].upper() in event_ticker:
                city_code = code
                break

        if not city_code:
            return None

        # Extract temperature range from title/subtitle
        # Patterns: "35°F to 39°F", "35 - 39", "above 40", "below 30"
        import re

        temp_low = None
        temp_high = None

        # Try range pattern: "XX°F to YY°F" or "XX - YY" or "XX to YY"
        range_match = re.search(r'(\d+)\s*(?:°F?\s*)?(?:to|-)\s*(\d+)', title + " " + subtitle)
        if range_match:
            temp_low = int(range_match.group(1))
            temp_high = int(range_match.group(2))
        else:
            # Try threshold pattern: "above XX" or "below XX" or "at least XX"
            # Also handle Kalshi subtitle patterns like "48° or above", "39° or below"
            above_match = re.search(r'(?:above|over|higher than|at least|≥|>=|>)\s*(\d+)', title + " " + subtitle, re.I)
            if not above_match:
                above_match = re.search(r'(\d+)°?\s*or\s*above', title + " " + subtitle, re.I)
            below_match = re.search(r'(?:below|under|lower than|less than|≤|<=|<)\s*(\d+)', title + " " + subtitle, re.I)
            if not below_match:
                below_match = re.search(r'(\d+)°?\s*or\s*below', title + " " + subtitle, re.I)

            if above_match:
                threshold = int(above_match.group(1))
                # Check if this was the inclusive "X or above" pattern vs strict "above X"
                matched_text = (title + " " + subtitle)[above_match.start():above_match.end()]
                if re.search(r'\d+°?\s*or\s*above', matched_text, re.I):
                    temp_low = threshold      # inclusive: "48° or above" → ≥48
                else:
                    temp_low = threshold + 1  # strict: "above 47°" → ≥48
                temp_high = 200  # Effectively infinity
            elif below_match:
                threshold = int(below_match.group(1))
                # Check if this was the inclusive "X or below" pattern vs strict "below X"
                matched_text = (title + " " + subtitle)[below_match.start():below_match.end()]
                if re.search(r'\d+°?\s*or\s*below', matched_text, re.I):
                    temp_high = threshold      # inclusive: "77° or below" → ≤77
                else:
                    temp_high = threshold - 1  # strict: "below 78°" → ≤77
                temp_low = -100  # Effectively negative infinity

        # Fallback: parse bucket/threshold directly from ticker suffix.
        # Ticker format: KXHIGH...-26FEB20-B77.5 (bucket) or -T55 (threshold)
        # This handles positions with empty titles (e.g., after reconciliation).
        if temp_low is None:
            bucket_match = re.search(r'-B(\d+\.?\d*)', ticker)
            thresh_match = re.search(r'-T(\d+\.?\d*)', ticker)
            if bucket_match:
                midpoint = float(bucket_match.group(1))
                temp_low = int(midpoint - 0.5)
                temp_high = int(midpoint + 0.5)
            elif thresh_match:
                threshold = int(float(thresh_match.group(1)))
                temp_low = threshold
                temp_high = 200
            else:
                return None

        # Extract date from event ticker (format: KXHIGHNY-26FEB12)
        date_match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})', event_ticker)
        if date_match:
            year = 2000 + int(date_match.group(1))
            month_str = date_match.group(2)
            day = int(date_match.group(3))
            months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
            month = months.get(month_str, 0)
            if month > 0:
                target_date = f"{year}-{month:02d}-{day:02d}"
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
        """
        Given a distribution and a temperature range, calculate the
        probability that the actual high falls in that range.

        Uses accuracy-weighted ensemble members when available.
        """
        if not distribution or not distribution.get("raw_highs"):
            return None

        highs = distribution["raw_highs"]
        weights = distribution.get("raw_weights")

        if weights and len(weights) == len(highs):
            # Weighted probability: more accurate models count more
            total_weight = sum(weights)
            if total_weight == 0:
                return None
            in_range_weight = sum(w for t, w in zip(highs, weights) if temp_low <= t <= temp_high)
            prob = in_range_weight / total_weight
        else:
            # Backward compatible: unweighted
            n = len(highs)
            if n == 0:
                return None
            in_range = sum(1 for t in highs if temp_low <= t <= temp_high)
            prob = in_range / n

        return prob


    def update_distribution_with_observation(self, distribution, obs_high, local_hour):
        """
        Bayesian update: shift the forecast distribution toward the observed high.
        Later in the day -> more weight on observation (temp is closer to final).

        Args:
            distribution: dict from get_temperature_distribution()
            obs_high: current observed high temp (Fahrenheit)
            local_hour: local hour (0-23) for the city

        Returns:
            Updated distribution dict (modified in place and returned).
        """
        if distribution is None or obs_high is None or local_hour < 10:
            return distribution

        forecast_mean = distribution.get('forecasted_high_mean')
        if forecast_mean is None:
            return distribution

        # Later in day = more weight on observation (max 0.8 at 6 PM)
        obs_weight = min(0.8, local_hour / 18.0)
        shift = (obs_high - forecast_mean) * obs_weight

        raw_highs = distribution.get('raw_highs')
        raw_weights = distribution.get('raw_weights')

        if raw_highs and len(raw_highs) > 1:
            # If obs is above forecast mean, truncate temps below obs
            # (temp already reached obs_high, can't un-happen for daily high)
            if obs_high > forecast_mean:
                adjusted = []
                adj_weights = []
                for i, t in enumerate(raw_highs):
                    if t >= obs_high:
                        adjusted.append(t + shift)
                    else:
                        # Lift below-obs temps up to at least obs_high
                        adjusted.append(max(obs_high, t + shift))
                    if raw_weights and i < len(raw_weights):
                        adj_weights.append(raw_weights[i])
                    else:
                        adj_weights.append(1.0)
            else:
                # Obs below forecast -- just shift everything
                adjusted = [t + shift for t in raw_highs]
                adj_weights = raw_weights if raw_weights else [1.0] * len(adjusted)

            # Recompute mean and std_dev from adjusted temps
            import math
            total_w = sum(adj_weights)
            if total_w > 0:
                new_mean = sum(t * w for t, w in zip(adjusted, adj_weights)) / total_w
                variance = sum(w * (t - new_mean) ** 2 for t, w in zip(adjusted, adj_weights)) / total_w
                new_std = math.sqrt(variance)
                distribution['forecasted_high_mean'] = round(new_mean, 1)
                distribution['std_dev'] = round(new_std, 1)
                distribution['raw_highs'] = sorted(adjusted)
                distribution['raw_weights'] = [w for _, w in sorted(zip(adjusted, adj_weights))]
        else:
            # No raw temps available -- just shift the mean
            distribution['forecasted_high_mean'] = round(forecast_mean + shift, 1)

        distribution['obs_adjusted'] = True
        distribution['obs_shift_f'] = round(shift, 2)
        return distribution

    # ═══════════════════════════════════════════════════════
    # STATUS: Print current state
    # ═══════════════════════════════════════════════════════

    def print_status(self):
        """Print what's currently cached."""
        if not self._cache:
            print("  [WEATHER] No forecasts cached yet")
            return

        for key, cached in self._cache.items():
            data = cached["data"]
            age_min = (datetime.now() - cached["fetched_at"]).total_seconds() / 60
            print(f"  [WEATHER] {data['city']} {data['target_date']}: "
                  f"mean={data['forecasted_high_mean']}°F, "
                  f"spread=±{data['std_dev']}°F, "
                  f"{data['total_members']} members, "
                  f"cached {age_min:.0f}min ago")
