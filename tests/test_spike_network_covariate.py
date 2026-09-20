"""The spike gate reads each region's own interconnectors, in its own direction.

Before this, ``covariates_for_interval`` looked up the Queensland to New South
Wales flow for every region and tested it against a single MW threshold. A
Victorian or South Australian interval was therefore scored on a link that
carries none of its imports, and because the id was absent from those regions'
interconnector sets the lookup returned nothing, so ``spike_credible`` was
None on every interval those regions ever pushed through the threshold. On a
live five region install that was 40 of 69 eligible intervals unanswerable by
construction. See issue #176.

What replaced it: net import across the region's own links, and the worst per
link import capability measured against that link's own median across the run.
The median is the reference because nominal capability spans an order of
magnitude between Basslink and Heywood, so no MW constant can serve them all.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.nem_pd7day.calibration_inputs import (  # noqa: E402
    import_capability,
    network_context,
    network_covariates_for_interval,
)
from custom_components.nem_pd7day.const import (  # noqa: E402
    REGION_INTERCONNECTOR_SIGN,
    REGION_INTERCONNECTORS,
    SPIKE_CAPABILITY_DEPRESSION,
)

FULL = 1000.0
BACKGROUND = 20


def flow(time_iso, mwflow, exportlimit=FULL, importlimit=-FULL):
    return types.SimpleNamespace(
        time=time_iso,
        mwflow=mwflow,
        exportlimit=exportlimit,
        importlimit=importlimit,
    )


def _iso(i):
    """Interval START i half hours into a fixed day, NEM time, UTC+10."""
    hour, minute = divmod(i * 30, 60)
    return "2026-09-21T%02d:%02d:00+10:00" % (hour % 24, minute)


TARGET = _iso(0)


def series(points):
    return types.SimpleNamespace(forecast=points)


def run_for(region, target_point_by_link):
    """One run of interconnector forecasts for ``region``.

    Every link carries BACKGROUND intervals at full capability so the median
    is well defined, plus whatever the caller specifies at TARGET.
    """
    links = {}
    for ic_id, sign in REGION_INTERCONNECTOR_SIGN[region].items():
        points = [target_point_by_link[ic_id]]
        for i in range(1, BACKGROUND + 1):
            points.append(flow(_iso(i), mwflow=sign * 400.0))
        links[ic_id] = series(points)
    return links


def covariates(region, target_point_by_link):
    context = network_context(run_for(region, target_point_by_link), region)
    return network_covariates_for_interval(context, TARGET)


# ── The mapping itself ───────────────────────────────────────────────────────


def test_every_region_has_a_direction_for_every_one_of_its_links():
    """The sign table and the interconnector sets must not drift apart.

    A link present in one and missing from the other is silent: the gate keeps
    returning a boolean, it just stops counting a path the region imports on.
    """
    assert set(REGION_INTERCONNECTOR_SIGN) == set(REGION_INTERCONNECTORS)
    for region, signs in REGION_INTERCONNECTOR_SIGN.items():
        assert set(signs) == set(REGION_INTERCONNECTORS[region]), (
            f"{region} sign map {sorted(signs)} does not match "
            f"interconnector set {sorted(REGION_INTERCONNECTORS[region])}"
        )
        assert set(signs.values()) <= {1, -1}, (
            f"{region} has a sign that is neither +1 nor -1: {signs}"
        )


def test_each_link_faces_opposite_ways_in_the_two_regions_it_joins():
    """One physical link, two regions, opposite directions.

    If both ends carried the same sign the same MW would read as an import in
    both regions at once, which is not a state the network can be in.
    """
    seen: dict[str, set[int]] = {}
    for signs in REGION_INTERCONNECTOR_SIGN.values():
        for ic_id, sign in signs.items():
            seen.setdefault(ic_id, set()).add(sign)
    for ic_id, signs in seen.items():
        assert signs == {1, -1}, (
            f"{ic_id} is signed {sorted(signs)}, expected one region each way"
        )


def test_the_hardcoded_queensland_link_no_longer_decides_other_regions():
    """VIC1 and SA1 must not be scored on the Queensland to New South Wales link."""
    for region in ("VIC1", "SA1", "TAS1"):
        assert "NSW1-QLD1" not in REGION_INTERCONNECTOR_SIGN[region]


# ── Import capability ────────────────────────────────────────────────────────


def test_capability_is_read_from_the_limit_that_faces_the_region():
    """AEMO publishes limits in the link's nominal direction, not the region's.

    Reading exportlimit for a link that runs out of the region would report
    the region's ability to export as its ability to import.
    """
    point = flow(TARGET, mwflow=0.0, exportlimit=700.0, importlimit=-400.0)
    assert import_capability(point, 1) == 700.0
    assert import_capability(point, -1) == 400.0


def test_a_missing_limit_is_none_and_not_zero():
    """An absent field is an unanswered question, not a closed interconnector."""
    point = types.SimpleNamespace(time=TARGET, mwflow=100.0)
    assert import_capability(point, 1) is None
    assert import_capability(point, -1) is None


# ── The gate ─────────────────────────────────────────────────────────────────


def test_depressed_capability_on_an_importing_region_reads_as_tight():
    """The condition the gate exists to find: importing, and the path is choked."""
    target = {
        "NSW1-QLD1": flow(TARGET, mwflow=400.0, exportlimit=50.0),
        "N-Q-MNSP1": flow(TARGET, mwflow=100.0),
    }
    result = covariates("QLD1", target)
    assert result["network_tight"] is True
    assert result["network_links_used"] == 2
    assert result["network_min_capability_ratio"] <= SPIKE_CAPABILITY_DEPRESSION
    assert result["network_net_import_mw"] == 500.0


def test_a_shut_link_reads_as_tight_without_needing_a_ratio():
    """Capability at or below zero is the limiting case, not a division problem."""
    target = {
        "NSW1-QLD1": flow(TARGET, mwflow=400.0, exportlimit=-24.0),
        "N-Q-MNSP1": flow(TARGET, mwflow=100.0),
    }
    assert covariates("QLD1", target)["network_tight"] is True


def test_full_capability_is_not_tight():
    target = {
        "NSW1-QLD1": flow(TARGET, mwflow=400.0),
        "N-Q-MNSP1": flow(TARGET, mwflow=100.0),
    }
    result = covariates("QLD1", target)
    assert result["network_tight"] is False
    assert result["network_min_capability_ratio"] == 1.0


def test_an_exporting_region_is_not_tight_even_with_a_choked_link():
    """A depressed export path is a different market condition.

    Without this guard the gate fires on the exporting side of a constraint,
    where the scarcity is not, which is most of what a naive utilisation test
    picks up over a week long horizon.
    """
    target = {
        "NSW1-QLD1": flow(TARGET, mwflow=-400.0, exportlimit=50.0),
        "N-Q-MNSP1": flow(TARGET, mwflow=-100.0),
    }
    result = covariates("QLD1", target)
    assert result["network_tight"] is False
    assert result["network_net_import_mw"] == -500.0


def test_the_threshold_is_the_boundary_it_says_it_is():
    """At exactly SPIKE_CAPABILITY_DEPRESSION the link counts as depressed."""
    reference_median = FULL
    at = reference_median * SPIKE_CAPABILITY_DEPRESSION
    just_above = at + 1.0
    for capability, expected in ((at, True), (just_above, False)):
        target = {
            "NSW1-QLD1": flow(TARGET, mwflow=400.0, exportlimit=capability),
            "N-Q-MNSP1": flow(TARGET, mwflow=100.0),
        }
        assert covariates("QLD1", target)["network_tight"] is expected, (
            f"capability {capability} against median {reference_median}"
        )


# ── Regions that could not previously be answered ────────────────────────────


def test_victoria_is_now_answerable_on_its_own_links():
    """Four links, three of them signed out of VIC1, one Basslink signed in."""
    target = {
        "VIC1-NSW1": flow(TARGET, mwflow=-500.0, importlimit=-50.0),
        "V-SA": flow(TARGET, mwflow=-200.0),
        "V-S-MNSP1": flow(TARGET, mwflow=-100.0),
        "T-V-MNSP1": flow(TARGET, mwflow=300.0),
    }
    result = covariates("VIC1", target)
    assert result["network_tight"] is True
    assert result["network_links_used"] == 4
    # 500 in from NSW1, 200 and 100 in from SA1, 300 in from TAS1
    assert result["network_net_import_mw"] == 1100.0


def test_south_australia_is_now_answerable_on_heywood_and_murraylink():
    target = {
        "V-SA": flow(TARGET, mwflow=600.0, exportlimit=100.0),
        "V-S-MNSP1": flow(TARGET, mwflow=50.0),
    }
    result = covariates("SA1", target)
    assert result["network_tight"] is True
    assert result["network_links_used"] == 2


def test_tasmania_is_answerable_on_basslink_alone():
    target = {"T-V-MNSP1": flow(TARGET, mwflow=-400.0, importlimit=-40.0)}
    result = covariates("TAS1", target)
    assert result["network_tight"] is True
    assert result["network_links_used"] == 1


# ── Missing data ─────────────────────────────────────────────────────────────


def test_a_missing_link_returns_none_and_reports_how_far_it_got():
    """Partial network data cannot rule a spike out.

    Returning False here would say the market was consulted and disagreed,
    when in fact one of the paths into the region was never read.
    """
    links = run_for(
        "QLD1",
        {
            "NSW1-QLD1": flow(TARGET, mwflow=400.0, exportlimit=50.0),
            "N-Q-MNSP1": flow(TARGET, mwflow=100.0),
        },
    )
    links["N-Q-MNSP1"] = series(
        [p for p in links["N-Q-MNSP1"].forecast if p.time != TARGET]
    )
    result = network_covariates_for_interval(
        network_context(links, "QLD1"), TARGET
    )
    assert result["network_tight"] is None
    assert result["network_min_capability_ratio"] is None
    assert result["network_net_import_mw"] is None


def test_an_unmapped_region_is_none_rather_than_a_guess():
    assert network_context({}, "XYZ1") is None
    assert network_context({}, None) is None
    assert network_covariates_for_interval(None, TARGET)["network_tight"] is None


def test_no_interconnector_data_at_all_is_none():
    context = network_context(
        {ic_id: series([]) for ic_id in REGION_INTERCONNECTOR_SIGN["QLD1"]},
        "QLD1",
    )
    assert network_covariates_for_interval(context, TARGET)["network_tight"] is None
