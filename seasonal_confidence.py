"""
SEASONAL FORECAST CONFIDENCE v1.0
=====================================
Scales position sizing based on empirically-grounded forecast reliability
by city, month, and weather regime.

The multiplier adjusts the base Quarter-Kelly position size up or down
depending on how predictable temperatures are for a given market.

Combined formula:
    final = seasonal_base[city][month] * regime_multiplier[detected_regime]
    final = clamp(final, SEASONAL_MULTIPLIER_MIN, SEASONAL_MULTIPLIER_MAX)
"""

import json
import os
from datetime import datetime
import config


# ═══════════════════════════════════════════════════════
# SEASONAL CONFIDENCE MATRIX
# ═══════════════════════════════════════════════════════
# Monthly multipliers (0.5–1.3) grounded in NWP model verification
# and meteorological reasoning. Index 0 = January, 11 = December.

SEASONAL_MATRIX = {
    "NYC": [1.10, 1.05, 0.75, 0.80, 0.90, 0.95, 1.05, 1.00, 0.85, 0.85, 1.00, 1.10],
    "CHI": [1.10, 1.00, 0.70, 0.75, 0.85, 0.90, 1.05, 1.00, 0.85, 0.80, 0.90, 1.10],
    "MIA": [1.10, 1.10, 1.05, 1.00, 0.85, 0.80, 0.85, 0.85, 0.75, 0.90, 1.05, 1.10],
    "AUS": [0.85, 0.80, 0.75, 0.80, 0.85, 1.00, 1.15, 1.15, 0.90, 0.80, 0.85, 0.85],
}

# ═══════════════════════════════════════════════════════
# REGIME OVERRIDE MULTIPLIERS
# ═══════════════════════════════════════════════════════
# Applied ON TOP of the seasonal base when a regime is detected.

REGIME_MULTIPLIERS = {
    "normal": 1.0,
    "anomalous_warm": 0.80,
    "anomalous_cold": 0.85,
    "frontal_passage": 0.70,
    "stable_high": 1.15,
    "tropical_influence": 0.65,
}

# ═══════════════════════════════════════════════════════
# CLIMATOLOGICAL NORMALS (daily high °F by month)
# ═══════════════════════════════════════════════════════

CLIMO_NORMALS = {
    "NYC": [39, 42, 50, 62, 72, 81, 85, 84, 76, 65, 54, 43],
    "CHI": [32, 36, 47, 59, 70, 80, 84, 82, 75, 62, 48, 35],
    "MIA": [76, 78, 80, 83, 87, 90, 91, 91, 89, 86, 82, 78],
    "AUS": [62, 66, 73, 80, 87, 93, 97, 98, 92, 83, 72, 63],
}

# File for persisting learned adjustments
SEASONAL_WEIGHTS_FILE = os.path.join(config.STATE_DIR, "seasonal_weights.json")


def _load_learned_weights():
    """Load empirically-adjusted weights from disk."""
    try:
        if os.path.exists(SEASONAL_WEIGHTS_FILE):
            with open(SEASONAL_WEIGHTS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_learned_weights(weights):
    """Persist learned weights to disk."""
    try:
        with open(SEASONAL_WEIGHTS_FILE, "w") as f:
            json.dump(weights, f, indent=2)
    except Exception:
        pass


def get_seasonal_multiplier(city, month, regime="normal"):
    """
    Returns a multiplier (0.5–1.3) applied to the Kelly-derived position size.

    Args:
        city: "NYC", "CHI", "MIA", "AUS"
        month: 1-12
        regime: "normal", "anomalous_warm", "anomalous_cold",
                "frontal_passage", "stable_high", "tropical_influence"

    Returns:
        float multiplier for position sizing
    """
    if not config.SEASONAL_SIZING_ENABLED:
        return 1.0

    # Get base seasonal multiplier (use learned weights if available)
    learned = _load_learned_weights()
    learned_city = learned.get(city, {})
    base = learned_city.get(str(month))

    if base is None:
        # Fall back to theoretical matrix
        city_multipliers = SEASONAL_MATRIX.get(city)
        if not city_multipliers:
            return 1.0
        base = city_multipliers[month - 1]

    # Apply regime override
    regime_mult = REGIME_MULTIPLIERS.get(regime, 1.0)
    final = base * regime_mult

    # Clamp to configured range
    return max(config.SEASONAL_MULTIPLIER_MIN, min(config.SEASONAL_MULTIPLIER_MAX, final))


def detect_regime(city, month, forecast_high):
    """
    Detect weather regime by comparing forecast to climatological normal.

    Args:
        city: "NYC", "CHI", "MIA", "AUS"
        month: 1-12
        forecast_high: forecasted high temperature in °F

    Returns:
        str regime name: "normal", "anomalous_warm", "anomalous_cold"
    """
    if not config.REGIME_DETECTION_ENABLED:
        return "normal"

    normals = CLIMO_NORMALS.get(city)
    if not normals or forecast_high is None:
        return "normal"

    normal = normals[month - 1]
    departure = forecast_high - normal

    if departure > config.ANOMALY_THRESHOLD_F:
        return "anomalous_warm"
    elif departure < -config.ANOMALY_THRESHOLD_F:
        return "anomalous_cold"
    return "normal"


def update_seasonal_multiplier(city, month, actual_win_rate, expected_win_rate, sample_size):
    """
    Nudge the seasonal multiplier based on empirical performance.
    Only adjusts after ADAPTIVE_LEARNING_MIN_TRADES trades in the bucket.

    Args:
        city: "NYC", "CHI", "MIA", "AUS"
        month: 1-12
        actual_win_rate: realized win rate (0-1)
        expected_win_rate: expected win rate based on edge (0-1)
        sample_size: number of trades in this city-month bucket
    """
    if sample_size < config.ADAPTIVE_LEARNING_MIN_TRADES:
        return

    if expected_win_rate <= 0:
        return

    performance_ratio = actual_win_rate / expected_win_rate

    # Get current base value
    city_multipliers = SEASONAL_MATRIX.get(city)
    if not city_multipliers:
        return
    current = city_multipliers[month - 1]

    # Blend: (1-α) theoretical + α empirical
    alpha = config.ADAPTIVE_LEARNING_BLEND
    empirical_adjustment = current * performance_ratio
    new_value = ((1 - alpha) * current) + (alpha * empirical_adjustment)

    # Clamp
    new_value = max(config.SEASONAL_MULTIPLIER_MIN, min(config.SEASONAL_MULTIPLIER_MAX, new_value))

    # Save to learned weights
    learned = _load_learned_weights()
    if city not in learned:
        learned[city] = {}
    learned[city][str(month)] = round(new_value, 3)
    _save_learned_weights(learned)


def get_all_multipliers():
    """Return current multipliers for all cities/months (for dashboard display)."""
    now = datetime.now()
    current_month = now.month
    learned = _load_learned_weights()

    result = {}
    for city in SEASONAL_MATRIX:
        base = SEASONAL_MATRIX[city][current_month - 1]
        # Check for learned override
        learned_val = learned.get(city, {}).get(str(current_month))
        if learned_val is not None:
            base = learned_val
        result[city] = {
            "base_multiplier": round(base, 2),
            "month": current_month,
            "is_learned": learned_val is not None,
        }
    return result
