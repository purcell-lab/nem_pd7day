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
CONF_REGION = "region"
CONF_REGIONS = "regions"  # kept for migration from old list-based config
CONF_FORECAST_MODE = "forecast_mode"
CONF_ACTIVE_TARIFF = "active_tariff"

# Forecast mode values
FORECAST_MODE_FULL = "days_1_7"       # naive: days 1-7, all residential tariffs visible
FORECAST_MODE_DAYS_2_7 = "days_2_7"   # sophisticated: days 2-7, one selected tariff
# TradingIS (actual price source)
TRADINGIS_BASE_URL = "https://www.nemweb.com.au/Reports/Current/TradingIS_Reports/"
DISPATCHIS_BASE_URL = "https://www.nemweb.com.au/Reports/Current/DispatchIS_Reports/"
ELEC_NEM_SUMMARY_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"
TRADINGIS_FETCH_MINUTES = [2, 32]

# Defaults
DEFAULT_REGION = "QLD1"


def get_region(entry) -> str:
    """Read region from config entry, handling migration from old list-based 'regions' key."""
    region = entry.options.get(CONF_REGION) or entry.data.get(CONF_REGION)
    if not region:
        old = entry.options.get(CONF_REGIONS) or entry.data.get(CONF_REGIONS, DEFAULT_REGION)
        region = old[0] if isinstance(old, list) else old
    return region or DEFAULT_REGION


# AEMO PD7DAY publish times (NEM local, hour, minute)
# Fetches are scheduled 25-55 min after each publish to allow NEMWeb to settle.
FETCH_TIMES_NEM = [(7, 30), (13, 0), (18, 0)]

# NEM time constants
NEM_TZ = timezone(timedelta(hours=10), name="AEST")
INTERVAL_DURATION = timedelta(minutes=30)

# Lifecycle tuning
REFIT_INTERVAL = timedelta(hours=24)

# Calibration engine tuning
MIN_OBS = 10
MAX_OBS = 5000
IRLS_ITER = 15
IRLS_EPS = 1e-8
QUANTILES = (0.1, 0.5, 0.9)
HORIZON_EDGES = [0, 6, 12, 24, 48, 96]
HORIZON_LABELS = ["h00_06", "h06_12", "h12_24", "h24_48", "h48_96", "h96plus"]
TOD_LABELS = ["shoulder", "morning_ramp", "solar", "peak"]
TOD_BUCKETS = {
    "solar": (10, 16),
    "peak": (16, 20),
    "shoulder": (20, 22),
    "offpeak": None,
}

# Calibration storage settings
STORAGE_VERSION = 1


def storage_keys(region: str) -> tuple[str, str, str]:
    """Return (obs_key, coeff_key, forecast_history_key) scoped to region."""
    r = region.lower()
    return (
        f"nem_pd7day.{r}.observation_log",
        f"nem_pd7day.{r}.calibration_coefficients",
        f"nem_pd7day.{r}.forecast_history",
    )


# Legacy (unscoped) key names — used only for one-time migration
_LEGACY_OBS_KEY = "nem_pd7day.observation_log"
_LEGACY_COEFF_KEY = "nem_pd7day.calibration_coefficients"
_LEGACY_FH_KEY = "nem_pd7day.forecast_history"
MAX_TOTAL_OBS = 20_000
MAX_FORECAST_AGE_DAYS = 14
MAX_HORIZON_HOURS = 168

# ── Spike covariate gating (Rec 2) ──────────────────────────────────────────
# When raw forecast > SPIKE_COVARIATE_RAW_FLOOR and the gas+QNI joint gate
# is NOT met (and horizon >= SPIKE_COVARIATE_BYPASS_HORIZON_H), cap the
# displayed value at SPIKE_COVARIATE_CAP and mark as non-passthrough.
SPIKE_GAS_THRESHOLD_TJ = 150.0       # gas_forecast_tj must exceed this
SPIKE_QNI_THRESHOLD_MW = -300.0      # qni_mwflow must be below (more negative than) this
SPIKE_COVARIATE_BYPASS_HORIZON_H = 12.0  # within 12h, trust raw forecast regardless
SPIKE_COVARIATE_CAP = 0.50           # $/kWh — cap when gate not met
SPIKE_COVARIATE_RAW_FLOOR = 1.00     # $/kWh — only apply gate above this raw value

# ── Horizon-dependent spike callout thresholds (Rec 1) ──────────────────────
# An interval is spike-callout eligible only if:
#   horizon < 24h AND raw >= SPIKE_CALLOUT_THRESHOLD_24H, OR
#   horizon < 48h AND raw >= SPIKE_CALLOUT_THRESHOLD_48H
# Beyond 48h: no callout ever.
SPIKE_CALLOUT_THRESHOLD_24H = 1.50   # $/kWh — moderate spikes within 24h
SPIKE_CALLOUT_THRESHOLD_48H = 3.00   # $/kWh — only extreme spikes at 24-48h

# Amber Express provides forecasts for the next 24h — PD7DAY forecast
# attribute is trimmed to horizons beyond this to avoid duplication.
AMBER_EXPRESS_HORIZON_H = 24.0

# ── Region → distributor → tariff mapping (for tariff forecast sensors) ────────
DISTRIBUTOR_DISPLAY_NAMES = {
    "energex":     "Energex",
    "ergon":       "Ergon",
    "ausgrid":     "Ausgrid",
    "endeavour":   "Endeavour Energy",
    "essential":   "Essential Energy",
    "evoenergy":   "Evoenergy",
    "jemena":      "Jemena",
    "powercor":    "Powercor",
    "united":      "United Energy",
    "ausnet":      "AusNet",
    "sapn":        "SA Power Networks",
    "tasnetworks": "TasNetworks",
    "victoria":    "Victoria",
}

REGION_DISTRIBUTORS = {
    "QLD1": ["energex", "ergon"],
    "NSW1": ["ausgrid", "endeavour", "essential"],
    "VIC1": ["jemena", "powercor", "united", "ausnet", "victoria"],
    "SA1":  ["sapn"],
    "TAS1": ["tasnetworks"],
}

DISTRIBUTOR_TARIFFS = {
    "energex":     ["8400", "3900", "3700", "6900", "8500", "3600", "3800", "6000", "6800", "6600", "6700", "7200", "8100", "8300", "8900", "8800", "94300"],
    "ergon":       ["6900", "3900", "ERTOUET1", "WRTOUET1", "MRTOUET4", "ERTDEMXT1", "ERTDEMCT1", "3600", "3800", "7200"],
    "ausgrid":     ["EA010", "EA025", "EA111", "EA116", "EA225", "EA305"],
    "endeavour":   ["N70", "N71", "N90", "N91", "N19", "N95", "N73", "N61"],
    "essential":   ["BLNREX2", "BLNBEX1", "BLNN2AU", "BLNT3AU", "BLNT3AL", "BLNRSS2", "BLND1AR", "BLNC1AU", "BLNC2AU", "BLNN1AU", "BLNT2AU", "BLNT2AL", "BLNT1AO", "BLNBSS1", "BLND1AB"],
    "evoenergy":   ["015", "016", "017", "018", "026", "090"],
    "jemena":      ["D1", "PRTOU"],
    "powercor":    ["D1", "PRTOU", "NDMO21", "NDTOU", "PRDS"],
    "united":      ["D1", "URTOU", "FURTOU", "FURDS", "URDS", "NDMO21", "NDTOU", "PRDS"],
    "ausnet":      ["NAST11S", "NEE11S"],
    "victoria":    ["VICR_SINGLE", "VICR_TOU", "VICR_DEMAND", "VICS_SINGLE", "VICS_TOU", "VICS_DEMAND"],
    "sapn":        ["RESELE", "RESELEX", "RELE2W", "SBELE", "SBELEX", "B2R", "RSR", "RTOU", "RTOUNE", "RPRO", "RELE", "SBTOU", "SBTOUNE"],
    "tasnetworks": ["TAS93", "TAS87", "TAS97", "TAS94", "TAS88"],
}

# ── Human-readable tariff names ──────────────────────────────────────────────
TARIFF_NAMES = {
    # Energex
    "energex": {
        "8400": "Residential Flat",
        "3900": "Residential Transitional Demand",
        "3700": "Residential Demand",
        "6900": "Residential Time of Use Energy",
        "3600": "Small Business Demand",
        "3800": "Small Business Transitional Demand",
        "6000": "Small Business Wide IFT",
        "8500": "Small Business Flat",
        "8900": "Small 8900 TOU",
        "8800": "Small 8800 TOU",
        "6800": "Small Business ToU Energy",
        "6600": "Large Residential Energy",
        "6700": "Large Business Energy",
        "7200": "LV Demand Time-of-Use",
        "8100": "Demand Large",
        "8300": "SAC Demand Small",
        "94300": "Large TOU Energy",
    },
    # Ergon
    "ergon": {
        "ERTOUET1": "Residential Battery ToU",
        "WRTOUET1": "Residential Wide ToU",
        "MRTOUET4": "Residential Multi ToU",
        "6900": "Residential Time of Use Energy",
        "ERTDEMXT1": "Residential Demand Extended",
        "ERTDEMCT1": "Residential Demand Controlled",
        "3900": "Residential Transitional Demand",
        "3600": "Small Business Demand",
        "3800": "Small Business Transitional Demand",
        "7200": "LV Demand Time-of-Use",
    },
    # Ausgrid
    "ausgrid": {
        "EA010": "Residential Flat",
        "EA025": "Residential ToU",
        "EA111": "Residential Demand Introductory",
        "EA116": "Residential Demand",
        "EA225": "Small Business ToU",
        "EA305": "Small Business LV",
    },
    # Endeavour
    "endeavour": {
        "N70": "Residential Flat",
        "N71": "Residential Seasonal TOU",
        "N90": "General Supply Block",
        "N91": "GS Seasonal TOU",
        "N19": "LV Seasonal STOU Demand",
        "N95": "Storage",
        "N73": "Residential Demand Transitional",
        "N61": "Residential Electrify",
    },
    # Essential Energy
    "essential": {
        "BLNREX2": "Residential Solar Export",
        "BLNBEX1": "Business Solar Export",
        "BLNN2AU": "Residential Anytime",
        "BLNT3AU": "Residential TOU Basic Meter",
        "BLNT3AL": "Residential TOU Interval Meter",
        "BLNRSS2": "Residential Sun Soaker",
        "BLND1AR": "Residential Demand",
        "BLNC1AU": "Controlled Load 1",
        "BLNC2AU": "Controlled Load 2",
        "BLNN1AU": "Small Business Anytime",
        "BLNT2AU": "Small Business TOU Basic Meter",
        "BLNT2AL": "Small Business TOU Interval Meter",
        "BLNT1AO": "Small Business TOU 100-160 MWh",
        "BLNBSS1": "Small Business Sun Soaker",
        "BLND1AB": "Small Business Demand",
    },
    # Evoenergy
    "evoenergy": {
        "015": "Residential TOU Network (Closed)",
        "016": "Residential TOU Network (Closed) XMC",
        "017": "New Residential TOU Network",
        "018": "New Residential TOU Network XMC",
        "026": "Residential Demand",
        "090": "Component Charge",
    },
    # Jemena
    "jemena": {
        "D1": "Residential Single Rate",
        "PRTOU": "Residential TOU",
    },
    # Powercor
    "powercor": {
        "D1": "Residential Single Rate",
        "PRTOU": "Residential TOU",
        "NDMO21": "NDMO21 TOU",
        "NDTOU": "NDTOU TOU",
        "PRDS": "Residential Daytime Saver",
    },
    # United Energy
    "united": {
        "D1": "Residential Single Rate",
        "URTOU": "Residential TOU",
        "FURTOU": "Residential Flexible TOU",
        "FURDS": "Residential Flexible Demand Saver",
        "URDS": "Residential Demand Saver",
        "NDMO21": "NDMO21 TOU",
        "NDTOU": "NDTOU TOU",
        "PRDS": "Residential Daytime Saver",
    },
    # AusNet
    "ausnet": {
        "NAST11S": "Small Business Time of Use",
        "NEE11S": "Residential Single Rate",
    },
    # SA Power Networks
    "sapn": {
        "RESELE": "Residential Electrify",
        "RESELEX": "Residential Electrify Export",
        "RELE2W": "Residential Electrify Two Way",
        "SBELE": "Small Business Electrify",
        "SBELEX": "Small Business Electrify Export",
        "B2R": "Business Two Rate",
        "RSR": "Residential Single Rate",
        "RTOU": "Residential Time of Use",
        "RTOUNE": "Residential TOU No Export",
        "RPRO": "Residential Prosumer",
        "RELE": "Residential Electrify",
        "SBTOU": "Small Business Time of Use",
        "SBTOUNE": "Small Business TOU No Export",
    },
    # TasNetworks
    "tasnetworks": {
        "TAS93": "Residential ToU Consumption",
        "TAS87": "Residential ToU Demand",
        "TAS97": "Residential ToU CER",
        "TAS94": "Small Business ToU Consumption",
        "TAS88": "Small Business ToU Demand",
    },
    # Victoria (generic)
    "victoria": {
        "VICR_SINGLE": "Residential Single Rate",
        "VICR_TOU": "Residential Time of Use",
        "VICS_SINGLE": "Small Business Single Rate",
        "VICS_TOU": "Small Business Time of Use",
        "VICR_DEMAND": "Residential Demand",
        "VICS_DEMAND": "Small Business Demand",
    },
}

# ── Default-enabled tariff sensors (residential ToU + trial per distributor) ──
DEFAULT_ENABLED_TARIFFS = {
    # Energex — Residential ToU + battery trial
    ("energex", "6900"),   # Residential Time of Use Energy
    ("energex", "8900"),   # Small 8900 TOU (trial)
    # Ergon
    ("ergon", "6900"),     # Residential Time of Use Energy
    ("ergon", "ERTOUET1"), # Residential Battery ToU
    # Ausgrid
    ("ausgrid", "EA025"),  # Residential ToU
    ("ausgrid", "EA111"),  # Residential Demand Introductory (trial)
    # Endeavour
    ("endeavour", "N71"),  # Residential Seasonal TOU
    ("endeavour", "N61"),  # Residential Electrify (trial)
    # Essential
    ("essential", "BLNT3AL"), # Residential TOU Interval Meter
    ("essential", "BLNRSS2"), # Residential Sun Soaker (trial)
    # Evoenergy
    ("evoenergy", "017"),  # New Residential TOU Network
    ("evoenergy", "018"),  # New Residential TOU Network XMC (trial)
    # Jemena — small set, enable both
    ("jemena", "D1"),
    ("jemena", "PRTOU"),
    # Powercor
    ("powercor", "PRTOU"), # Residential TOU
    ("powercor", "PRDS"),  # Residential Daytime Saver (trial)
    # United
    ("united", "URTOU"),   # Residential TOU
    ("united", "PRDS"),    # Residential Daytime Saver (trial)
    # AusNet — small set, enable both
    ("ausnet", "NAST11S"),
    ("ausnet", "NEE11S"),
    # SA Power Networks
    ("sapn", "RTOU"),      # Residential Time of Use
    ("sapn", "RPRO"),      # Residential Prosumer (trial)
    ("sapn", "RESELE"),    # Residential Electrify
    # TasNetworks
    ("tasnetworks", "TAS93"), # Residential ToU Consumption
    ("tasnetworks", "TAS97"), # Residential ToU CER (trial)
    # Victoria
    ("victoria", "VICR_TOU"),    # Residential Time of Use
    ("victoria", "VICR_DEMAND"), # Residential Demand (trial)
}

# ── Export tariff programs (import_code → export_code per distributor) ────────
# Each entry maps (distributor, import_tariff_code) → export_tariff_code.
# Export sensors use spot_to_feed_in_tariff() instead of spot_to_tariff().
EXPORT_TARIFF_PROGRAMS = {
    ("ausgrid", "EA025"): "EA029",
    ("endeavour", "N71"): "N61",
    ("essential", "BLNT3AL"): "BLNREX2",
    ("sapn", "RESELE"): "RESELE",
}

# Human-readable export tariff names (export_code → name)
EXPORT_TARIFF_NAMES = {
    "EA029": "Residential Electrify",
    "N61": "Residential Electrify",
    "BLNREX2": "LV Residential Solar Export",
    "RESELE": "Residential Electrify",
}

# Coordinator / store keys
COORDINATOR_KEY = "coordinator"
STORE_KEY = "store"
DISPATCH_KEY = "dispatch"

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
ATTR_CAL_MAE = "ols_mae"
ATTR_CAL_SOURCE = "calibrated_source"
ATTR_CAL_N_OBS = "n_obs"

# ── Additional usage fee ─────────────────────────────────────────────────────
DEFAULT_ADDITIONAL_FEE = 0.0293


def additional_fee_entity_id(region: str) -> str:
    """Return the entity_id for the AdditionalFeeNumber for a given region."""
    return f"number.nem_pd7day_{region.lower()}_additional_usage_fee"
