"""Constants for the NEM PD7DAY integration."""

import re
from datetime import timedelta, timezone

DOMAIN = "nem_pd7day"

# NEMWeb base URL for PD7DAY reports
NEMWEB_BASE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/PD7Day/"
FILE_PATTERN = re.compile(r"PUBLIC_PD7DAY_.*\.(ZIP|CSV)$", re.IGNORECASE)
AEMO_WWW = "https://aemo.com.au/"
ATTR_ATTRIBUTION = "Data provided by AEMO"
DEVICE_MANUFACTURER = "Mark Purcell"
DEVICE_MODEL = "PD7DAY"
DEVICE_CONFIGURATION_URL = AEMO_WWW

# Interconnectors per region
QLD1_INTERCONNECTORS = {"NSW1-QLD1", "N-Q-MNSP1"}
NSW1_INTERCONNECTORS = {"NSW1-QLD1", "VIC1-NSW1", "N-Q-MNSP1"}
VIC1_INTERCONNECTORS = {"VIC1-NSW1", "SA1-VIC1", "V-S-MNSP1", "T-V-MNSP1"}
SA1_INTERCONNECTORS = {"V-SA", "V-S-MNSP1"}
TAS1_INTERCONNECTORS = {"T-V-MNSP1"}

REGION_INTERCONNECTORS = {
    "QLD1": QLD1_INTERCONNECTORS,
    "NSW1": NSW1_INTERCONNECTORS,
    "VIC1": VIC1_INTERCONNECTORS,
    "SA1": SA1_INTERCONNECTORS,
    "TAS1": TAS1_INTERCONNECTORS,
}


def interconnectors_for_regions(regions: list[str]) -> set[str]:
    """Return the union of interconnectors for all selected regions."""
    interconnectors: set[str] = set()
    for region in regions:
        interconnectors.update(REGION_INTERCONNECTORS.get(region, set()))
    return interconnectors


# Supported NEM regions
REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]

# Config entry keys
CONF_REGIONS = "regions"
CONF_CALIBRATION_REGION = "calibration_region"
CONF_AMBER_SENSOR = "amber_sensor"

# TradingIS (actual price source)
TRADINGIS_BASE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"
TRADINGIS_FETCH_MINUTES = [2, 32]

# Defaults
DEFAULT_REGIONS = ["QLD1"]

# AEMO PD7DAY publish times (NEM local, hour, minute)
# Fetches are scheduled 25-55 min after each publish to allow NEMWeb to settle.
FETCH_TIMES_NEM = [(7, 30), (13, 0), (18, 0)]

# NEM time constants
NEM_TZ = timezone(timedelta(hours=10), name="AEST")
INTERVAL_DURATION = timedelta(minutes=30)

# Lifecycle tuning
AMBER_ACTUAL_ENTITY = "sensor.amber_express_amber_feed_in_price"
DEFAULT_CALIBRATION_REGION = "QLD1"
REFIT_INTERVAL = timedelta(hours=24)

# Calibration engine tuning
MIN_OBS = 10
MAX_OBS = 5000
IRLS_ITER = 15
IRLS_EPS = 1e-8
QUANTILES = (0.1, 0.5, 0.9)
MAX_INTERCEPT_ABS = 1.0
MAX_CALIBRATED_RATIO = 5.0
HORIZON_EDGES = [0, 6, 12, 24, 48, 96]
HORIZON_LABELS = ["h00_06", "h06_12", "h12_24", "h24_48", "h48_96", "h96plus"]
TOD_BUCKETS = {
    "solar": (10, 16),
    "peak": (16, 20),
    "shoulder": (20, 22),
    "offpeak": None,
}

# Calibration storage settings
OBS_STORAGE_KEY = "nem_pd7day.observation_log"
COEFF_STORAGE_KEY = "nem_pd7day.calibration_coefficients"
FORECAST_HISTORY_STORAGE_KEY = "nem_pd7day.forecast_history"
STORAGE_VERSION = 1
MAX_TOTAL_OBS = 20_000
MAX_FORECAST_AGE_DAYS = 14
MAX_HORIZON_HOURS = 168

# Coordinator / store keys
COORDINATOR_KEY = "coordinator"
STORE_KEY = "store"

# ── PRICESOLUTION sensor attributes ──────────────────────────────────────────
ATTR_REGION = "region"
ATTR_FORECAST_GENERATED_AT = "forecast_generated_at"
ATTR_INTERVAL_MINUTES = "interval_minutes"
ATTR_NEXT_VALUE = "next_value"
ATTR_MIN_24H = "min_24h_value"
ATTR_MAX_24H = "max_24h_value"
ATTR_CHEAPEST_2H = "cheapest_2h_window"
ATTR_FORECAST = "forecast"
ATTR_SOURCE_FILE = "source_file"

# ── CASESOLUTION binary sensor attributes ─────────────────────────────────────
ATTR_RUN_DATETIME = "run_datetime"
ATTR_LAST_CHANGED = "last_changed"

# ── MARKET_SUMMARY sensor attributes ─────────────────────────────────────────
ATTR_CURRENT_TJ = "current_tj"
ATTR_MAX_7D_TJ = "max_7d_tj"
ATTR_GAS_FORECAST = "forecast"

# ── INTERCONNECTORSOLUTION sensor attributes ──────────────────────────────────
ATTR_INTERCONNECTOR_ID = "interconnector_id"
ATTR_MWFLOW = "mwflow"
ATTR_METEREDMWFLOW = "meteredmwflow"
ATTR_MWLOSSES = "mwlosses"
ATTR_MARGINALVALUE = "marginalvalue"
ATTR_VIOLATIONDEGREE = "violationdegree"
ATTR_EXPORTLIMIT = "exportlimit"
ATTR_IMPORTLIMIT = "importlimit"
ATTR_IS_CONSTRAINED = "is_constrained"
ATTR_MAX_VIOLATION_7D = "max_violation_7d"
ATTR_IC_FORECAST = "forecast"

# ── Calibration sensor attributes ─────────────────────────────────────────────
ATTR_CAL_STATUS = "status"
ATTR_CAL_FITTED_AT = "fitted_at"
ATTR_CAL_OBS_COUNT = "observation_count"
ATTR_CAL_ACTIVE_BUCKETS = "active_buckets"
ATTR_CAL_TOTAL_BUCKETS = "total_buckets"
ATTR_CAL_SUMMARY = "summary"

# ── Calibrated forecast attributes ────────────────────────────────────────────
ATTR_CAL_CALIBRATED = "calibrated"
ATTR_CAL_P10 = "p10"
ATTR_CAL_P50 = "p50"
ATTR_CAL_P90 = "p90"
ATTR_CAL_MAE = "mae"
ATTR_CAL_SOURCE = "calibrated_source"
ATTR_CAL_N_OBS = "n_obs"
