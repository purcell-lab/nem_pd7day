"""
7-day forecast chart rendered from coordinator price data.

Pure computation — no HA dependencies, fully testable.

render_forecast_chart(forecast_data, region) -> bytes (PNG)

The chart shows:
  - Raw PD7day forecast line (grey, thin)
  - Calibrated forecast line (dark blue, bold) with confidence-tier styling (Rec 5)
  - p10/p90 confidence band (light blue shaded, opacity by horizon)
  - ToD background shading (shoulder/morning_ramp/solar/peak)
  - Horizon-gated spike callouts (Rec 1): suppressed beyond 48h
  - Spike persistence styling (Rec 4): confirmed vs candidate callouts
  - 24h/72h confidence boundary lines (Rec 5)
"""
from __future__ import annotations

import io
import logging
import struct
import zlib
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

def _placeholder_png(message: str = "") -> bytes:
    """Return a minimal 1x1 white PNG when matplotlib is unavailable."""
    _SIGNATURE = b'\x89PNG\r\n\x1a\n'

    def _chunk(name: bytes, data: bytes) -> bytes:
        c = struct.pack('>I', len(data)) + name + data
        return c + struct.pack('>I', zlib.crc32(name + data) & 0xFFFFFFFF)

    ihdr = _chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
    # 1x1 white pixel: filter byte 0x00 + RGB 0xFF 0xFF 0xFF
    idat = _chunk(b'IDAT', zlib.compress(b'\x00\xff\xff\xff'))
    iend = _chunk(b'IEND', b'')
    return _SIGNATURE + ihdr + idat + iend


# ToD background colours (very light fills)
_TOD_COLORS = {
    "shoulder": "#F5F5F5",
    "morning_ramp": "#FFF8E1",
    "solar": "#FFFDE7",
    "peak": "#FFF0F0",
}

# Y-axis clip for passthrough_high intervals
_PASSTHROUGH_CLIP = 2.0

# Spike callout thresholds (Rec 1) — imported from const at module level
# to keep the chart renderer self-contained for testing, these are also
# defined here as defaults and can be overridden by the data dict.
_SPIKE_CALLOUT_THRESHOLD_24H = 1.50  # $/kWh
_SPIKE_CALLOUT_THRESHOLD_48H = 3.00  # $/kWh

# Spike callout layout. Until issue #84 the camera never set spike_credible, so
# no chart had ever drawn one of these boxes and none of the numbers below had
# ever been checked against a rendered image. They were measured by rendering
# synthetic runs with a non-empty credible set and reading back the placed
# artists, and each one fixes a defect that measurement found.
#
# Spikes closer together than this share one label. The old 60 min window split
# a single evening episode into several clusters whose boxes then landed on top
# of each other; the triangles still mark every interval individually.
_CALLOUT_CLUSTER_GAP_MIN = 360.0
# Everything drawn above the clip line shares one narrow strip, and the strip
# has to be allocated in points rather than in fractions of the price axis.
# Anything positioned as a fraction of CLIP_Y moves whenever the y limits move,
# and the y limits now move: the headroom reservation below raises the axis top
# when a callout is present, which drags a data positioned label down onto
# whatever sits at a fixed point offset beneath it. That is what happened when
# the callouts were first switched on. The clip label sat at CLIP_Y * 1.02 and
# the grid stress labels at CLIP_Y * 1.12, 1.20 and 1.28, and raising the axis
# top pulled the clip label into the daily maximum label, which is drawn 9 pt
# above its marker. On main the two clear each other by a tenth of a pixel,
# which is luck rather than design, so both label families are now positioned
# in points and the strip is allocated once, here, bottom to top:
#
#   9 pt   daily maximum and minimum labels (unchanged, set at the draw site)
#  17 pt   the clip line label
#  28 pt   grid stress notice labels, three tiers when notices overlap in time
#  58 pt   spike callout boxes, two tiers
#
# Each entry allows about 7.5 pt for a line of text and the callout boxes about
# 13.5 pt including their padding.
_CLIP_LABEL_OFFSET_PT = 17
_NOTICE_LABEL_OFFSETS_PT = (28, 38, 48)
# Vertical tiers, in points above the clip line, tried in order. These sit above
# the notice label tiers so a callout and a notice label cannot collide
# vertically at all. Two tiers rather than three: with the boxes now fanned out
# sideways and placed by measurement, the third tier bought almost nothing, and
# dropping it lowers the reserved headroom, which is what compresses the price
# curve on exactly the days a spike is forecast. Measured on the seven day
# example fixture the axis top falls from 1.70 to 1.54 times the clip value,
# against 1.35 with no callout at all, and the cost across a 150 chart sweep is
# two labels of 240 that lose their leader line.
_CALLOUT_Y_OFFSETS_PT = (58, 76)
# Horizontal offsets, in points, tried in order within each tier. Fixing the
# strip allocation stopped the boxes landing on other labels but not the leader
# lines, which still crossed them: with only a 32 pt sideways offset available a
# line rising out of the clip line strip stays nearly vertical, and everything
# it has to avoid is stacked vertically right there. Fanning the box out
# sideways lets the line leave the strip within a few pixels of its start. The
# long offsets earn their place: the clip line label is about 138 px wide and can
# sit 11 px above the point a callout aims at, so a line escaping it sideways
# needs to cover about 3.7 px across for every 1 px up, and 184 pt only buys 3.2.
_CALLOUT_X_OFFSETS_PT = (32, 64, 100, 140, 184, 240, 300)
# Offsets tried when no leader line placement is free at all. The box goes
# beside its own marker with the line switched off, rather than a line being
# drawn across somebody else's text.
_CALLOUT_DIRECT_OFFSETS_PT = (
    (12, 4), (-12, 4), (12, 18), (-12, 18), (26, -14), (-26, -14),
)
# Rectangles are inflated by this many pixels before the overlap test, so a box
# that merely grazes a label still counts as a collision.
_CALLOUT_COLLISION_MARGIN_PX = 2.0
# A leader line that starts inside a label crosses it whichever way it leaves,
# and the point a callout naturally aims at, the clip line at the spike, is
# underneath the daily maximum label of that very interval and often the clip
# line label as well. So the start of the line is raised clear of anything that
# encloses it, by at most this much, which keeps it below the lowest callout
# tier. The line then arrives just above the marker rather than on it, in the
# same vertical, and crosses nothing.
_CALLOUT_ANCHOR_LIFT_MAX_PT = 48.0
_CALLOUT_ANCHOR_LIFT_PASSES = 4
# Fraction of the visible y span kept clear above the clip line when callouts
# are present. The tier offsets are a fixed number of points but the y span is
# not: a run whose p10 reaches the -$1000/MWh market floor stretches the axis
# far enough that the top offset lands outside it, and the box was then painted
# over the title. Measured overflow before this reservation: 55 px. The top of
# the upper callout box sits about 90 pt above the clip line and the axes are
# about 371 pt tall, so 24 per cent is the minimum that fits and this leaves
# margin for the topmost y tick label. The ratio of points to axis fraction does
# not depend on dpi, because the axes height in points does not either.
_CALLOUT_HEADROOM_FRAC = 0.30

# ── Measured placement for the labels that are not callouts, issue #93 ───────
# The daily extreme labels, the day divider labels and the confidence boundary
# label were all drawn at one fixed offset from their anchor with nothing
# checking what was already there, so they landed on the right hand $/MWh tick
# labels, on the legend box, on the x axis tick labels and on each other. They
# now go through the same machinery the spike callouts use: real bounding boxes
# off the drawn canvas, a fan of candidate offsets tried nearest first, and the
# first placement that sits inside the axes and clear of everything wins.
#
# The default offset is the first candidate in every fan, so a label with room
# where it always sat does not move at all. Only a colliding label moves.
#
# Sideways magnitudes in points, tried in this order within a vertical tier. The
# cap of 88 pt, about 122 px, was set by looking at rendered charts rather than
# by taste. A daily extreme label has no leader line, so the reader ties it to a
# dot by proximity alone. Letting the search run out to 188 pt did drive the
# measured collisions to zero, and the chart it produced was worse: on a run
# whose legend fills the top right, the last day's labels were shifted a full day
# left and sat next to the previous day's dots, reading as their values. A label
# that is dropped costs the reader a number. A label next to the wrong dot tells
# them a false one, so the fan stops here and the placement degrades instead.
_LABEL_X_OFFSETS_PT = (0, 10, 20, 32, 46, 64, 88)
# Vertical tiers away from the marker, added to the family's own base offset.
# These run further than the sideways magnitudes on purpose: a label directly
# above or below its own dot is in the dot's own column and cannot be read as
# belonging to a different day, so distance costs much less attribution.
_LABEL_Y_STEPS_PT = (0, 8, 16, 25, 35, 48, 63, 80, 100, 124)
# Sideways magnitudes for a label placed beside its marker at the marker's own
# height. The first is large enough to clear the 30 pt marker dot.
_LABEL_SIDE_OFFSETS_PT = (11, 20, 32, 46, 64, 88)
# Costs, which decide the order the fan is tried in. Sideways is weighted four
# times a vertical tier and the far side of the marker is cheaper than a long
# sideways shift, both for the same reason: a label directly above or below its
# own dot is unambiguous however far away it is, while a label shifted sideways
# drifts toward the next day's dot and can be read as that day's price. Measured
# on the maintainer's fixture, a flat cost let the Mon 7 Sep maximum escape the
# legend by moving 88 pt left, which put it across the day divider and beside
# Sun 6 Sep's dot. Weighting the sideways moves puts it under its own dot instead.
_LABEL_COST_X_WEIGHT = 4.0
_LABEL_COST_FLIP = 12.0
_LABEL_COST_SIDE = 22.0
_LABEL_COST_SIDE_X_WEIGHT = 3.0
# Day divider label, points to the right of the midnight line. This was 0.02 in
# date units, which is 29 minutes and so a different pixel gap on every chart
# width; a 12 interval chart put the label a third of the way across itself.
_DIVIDER_LABEL_OFFSET_PT = 4
_DIVIDER_X_OFFSETS_PT = (4, 9, 15, 24, 36)
_DIVIDER_Y_STEPS_PT = (0, 10, 20, 31, 43)
# The confidence boundary label straddles its own vertical line and reads
# "reliable" to the left and "uncertain" to the right, so it has to keep the line
# inside itself to mean anything. It slides down the line freely, and sideways
# only by less than half its own width, which is enforced against the measured
# width rather than assumed.
_ZONE_LABEL_Y_STEPS_PT = (0, -10, -20, -31, -43, -56, -72, -90, -110, -132,
                          -156, -182)
_ZONE_LABEL_X_OFFSETS_PT = (0, 8, 16, 26, 38, 52)


def _tod_label(hour: int) -> str:
    """Classify an hour into a time-of-day label."""
    if 6 <= hour < 9:
        return "morning_ramp"
    if 9 <= hour < 16:
        return "solar"
    if 16 <= hour < 21:
        return "peak"
    return "shoulder"


def _is_spike_callout_eligible(raw_value: float, horizon_hours: float, spike_first_run: bool) -> tuple[bool, str]:
    """Determine if an interval qualifies for spike callout display (Rec 1 + Rec 4).

    Returns (eligible, style) where style is one of:
      - "confirmed" — solid red callout (appeared in prior run too)
      - "candidate" — light grey callout (first run only)
      - "" — not eligible
    """
    # Rec 1: horizon gating — no callouts beyond 48h ever
    if horizon_hours >= 48:
        return False, ""
    # Rec 1: threshold depends on horizon
    if horizon_hours < 24:
        if raw_value < _SPIKE_CALLOUT_THRESHOLD_24H:
            return False, ""
    else:
        # 24-48h range
        if raw_value < _SPIKE_CALLOUT_THRESHOLD_48H:
            return False, ""
    # Rec 4: persistence check — first-run spikes are candidates, not confirmed
    if spike_first_run:
        return True, "candidate"
    return True, "confirmed"


def _as_rect(bbox) -> tuple[float, float, float, float]:
    """Normalise a matplotlib Bbox to an ordered (x0, y0, x1, y1) tuple."""
    return (
        min(bbox.x0, bbox.x1), min(bbox.y0, bbox.y1),
        max(bbox.x0, bbox.x1), max(bbox.y0, bbox.y1),
    )


def _rects_overlap(a, b, margin: float = _CALLOUT_COLLISION_MARGIN_PX) -> bool:
    """True when two (x0, y0, x1, y1) rectangles overlap, allowing a margin."""
    return (
        a[0] - margin < b[2] and b[0] - margin < a[2]
        and a[1] - margin < b[3] and b[1] - margin < a[3]
    )


def _segment_rect_clip(p0, p1, rect, margin: float = _CALLOUT_COLLISION_MARGIN_PX):
    """Clip a segment against a rectangle, Liang Barsky.

    Returns the (t0, t1) parameter interval of the part of p0 to p1 that lies
    inside the rectangle, or None when the segment misses it entirely.
    """
    x0, y0 = p0
    dx, dy = p1[0] - x0, p1[1] - y0
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, x0 - (rect[0] - margin)),
        (dx, (rect[2] + margin) - x0),
        (-dy, y0 - (rect[1] - margin)),
        (dy, (rect[3] + margin) - y0),
    ):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    if t0 > t1:
        return None
    return (t0, t1)


def _segments_cross(a0, a1, b0, b1) -> bool:
    """True when two segments properly cross, used to keep leader lines apart."""
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = side(a0, a1, b0), side(a0, a1, b1)
    d3, d4 = side(b0, b1, a0), side(b0, b1, a1)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def text_obstacle_rects(axes_list, renderer, exclude=()) -> list:
    """Collect the display space rectangles of every text artist on the axes.

    This is what callout placement has to route around, and the tests call the
    same function so the assertion measures what the placement measured. A text
    that carries a bbox patch is measured from the patch, because that painted
    box is what the reader sees. An Annotation window extent would swallow its
    own arrow as well and overstate the label.
    """
    skip = {id(a) for a in exclude}
    rects = []
    for ax in axes_list:
        if ax is None:
            continue
        candidates = list(ax.texts)
        candidates.append(ax.title)
        candidates.extend(ax.get_xticklabels())
        candidates.extend(ax.get_yticklabels())
        for art in candidates:
            if art is None or id(art) in skip:
                continue
            if not art.get_visible() or not str(art.get_text()).strip():
                continue
            patch = art.get_bbox_patch() if hasattr(art, "get_bbox_patch") else None
            try:
                bbox = (patch or art).get_window_extent(renderer)
            except (RuntimeError, ValueError, AttributeError):
                continue
            if bbox.width <= 0 or bbox.height <= 0:
                continue
            rects.append(_as_rect(bbox))
        legend = ax.get_legend()
        if legend is not None and legend.get_visible():
            try:
                rects.append(_as_rect(legend.get_window_extent(renderer)))
            except (RuntimeError, ValueError):
                pass
    return rects


def callout_box_rect(art, renderer):
    """The painted label box of a callout annotation, in display coordinates."""
    patch = art.get_bbox_patch()
    if patch is None:
        return None
    return _as_rect(patch.get_window_extent(renderer))


def callout_leader_segment(ax, art, renderer):
    """The visible part of a callout leader line, in display coordinates.

    Returns None when the arrow is switched off, which is how a callout that
    could not be placed cleanly degrades. The segment is trimmed at the label
    box border so the stretch hidden behind the box is not reported as a line
    crossing something.
    """
    arrow = getattr(art, "arrow_patch", None)
    if arrow is None or not arrow.get_visible():
        return None
    box = callout_box_rect(art, renderer)
    if box is None:
        return None
    x = ax.xaxis.convert_units(art.xy[0])
    y = ax.yaxis.convert_units(art.xy[1])
    anchor = tuple(ax.transData.transform((x, y)))
    centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    clipped = _segment_rect_clip(anchor, centre, box, margin=0.0)
    t = clipped[0] if clipped is not None else 1.0
    end = (anchor[0] + t * (centre[0] - anchor[0]),
           anchor[1] + t * (centre[1] - anchor[1]))
    return (anchor, end)


def count_callout_text_collisions(fig, ax, callout_artists, other_axes=()):
    """Count callout collisions with the rest of the chart text.

    Returns (line_over_text, box_over_text). Both must be zero. The first
    revision of this work compared callout boxes only with each other, which is
    why it reported no overlaps on a chart whose leader line ran straight
    through the clip line annotation and through a daily maximum label.
    """
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    axes_list = [ax] + [a for a in other_axes if a is not None]
    obstacles = text_obstacle_rects(axes_list, renderer, exclude=callout_artists)
    boxes = [callout_box_rect(a, renderer) for a in callout_artists]
    obstacles += [b for b in boxes if b is not None]
    line_hits = 0
    box_hits = 0
    for i, art in enumerate(callout_artists):
        own = boxes[i]
        for rect in obstacles:
            if own is not None and rect is own:
                continue
            if own is not None and _rects_overlap(own, rect):
                box_hits += 1
        seg = callout_leader_segment(ax, art, renderer)
        if seg is None:
            continue
        for rect in obstacles:
            if own is not None and rect is own:
                continue
            if _segment_rect_clip(seg[0], seg[1], rect) is not None:
                line_hits += 1
    return line_hits, box_hits


def _lift_anchor(anchor, obstacles, scale, corridor=False):
    """Raise the start of a leader line clear of the labels stacked over it.

    The point a callout naturally aims at, the clip line at the spike interval,
    is underneath the daily maximum label of that very interval, and a line
    starting inside a label crosses it whichever way it leaves. So the start is
    raised clear of whatever encloses it, repeatedly, since clearing one label
    can leave the start inside the next one up. Only enclosing labels are
    cleared, unless corridor is set, in which case labels stacked higher up the
    same vertical are cleared too. Both are tried: sideways is usually the better
    escape, but a wide label like the clip line annotation can sit a few pixels
    above the marker and span a tenth of the chart, and then no angle is steep
    enough and the line has to start above it instead. Past the cap the anchor is
    left alone and the placement is allowed to fail into a direct label, because
    a line that starts that high is no longer pointing at its own marker.
    """
    limit = anchor[1] + _CALLOUT_ANCHOR_LIFT_MAX_PT * scale
    lifted = anchor[1]
    for _pass in range(_CALLOUT_ANCHOR_LIFT_PASSES):
        tops = [
            rect[3] + _CALLOUT_COLLISION_MARGIN_PX + 1.0
            for rect in obstacles
            if rect[0] <= anchor[0] <= rect[2]
            and (rect[1] <= lifted <= rect[3]
                 or (corridor and lifted <= rect[1] <= limit))
        ]
        if not tops:
            break
        nxt = max(tops)
        if nxt <= lifted:
            break
        lifted = nxt
    if anchor[1] < lifted <= limit:
        return (anchor[0], lifted)
    return anchor


def _callout_candidates(anchor, ax_rect):
    """Offsets to try, nearest first, preferring the roomier side of the axes."""
    prefer_right = anchor[0] - ax_rect[0] <= ax_rect[2] - anchor[0]
    signs = (1, -1) if prefer_right else (-1, 1)
    out = []
    for yi, yoff in enumerate(_CALLOUT_Y_OFFSETS_PT):
        for xi, mag in enumerate(_CALLOUT_X_OFFSETS_PT):
            for si, sign in enumerate(signs):
                out.append((xi + 0.6 * yi + 0.3 * si, sign * mag, yoff))
    out.sort(key=lambda c: c[0])
    return out


def _plan_callout_placements(order, metrics, fixed_rects, ax_rect, scale,
                             corridor=False):
    """Work out where each callout goes, for one order of consideration.

    Pure geometry, nothing is drawn or moved, so several orders can be costed
    and the best one applied. Returns {index: decision}.
    """
    obstacles = list(fixed_rects)
    segments: list = []
    plan: dict = {}
    for i in order:
        m = metrics[i]
        # Lifting reads only the fixed text, never the callouts placed so far,
        # so a callout's anchor does not depend on the order.
        anchor = _lift_anchor(m["anchor"], fixed_rects, scale,
                              corridor=corridor)
        width, height = m["width"], m["height"]
        pad_x, pad_y = m["pad_x"], m["pad_y"]

        def rect_for(xoff, yoff):
            point_x = anchor[0] + xoff * scale
            point_y = anchor[1] + yoff * scale
            x0 = point_x - pad_x if xoff >= 0 else point_x + pad_x - width
            y0 = point_y - pad_y
            return (x0, y0, x0 + width, y0 + height)

        def inside(rect):
            return (ax_rect[0] <= rect[0] and rect[2] <= ax_rect[2]
                    and ax_rect[1] <= rect[1] and rect[3] <= ax_rect[3])

        chosen = None
        # The first pass also refuses to cross another leader line. The second
        # allows it, because a crossed line is still readable whereas a label
        # sitting on its own tells the reader less.
        for allow_crossing in (False, True):
            for cost, xoff, yoff in _callout_candidates(anchor, ax_rect):
                rect = rect_for(xoff, yoff)
                if not inside(rect):
                    continue
                if any(_rects_overlap(rect, o) for o in obstacles):
                    continue
                # A box must also keep off the leader lines already drawn, not
                # only their boxes. Checking one direction only left three charts
                # in a 150 chart sweep with a later box sitting on an earlier
                # line.
                if any(_segment_rect_clip(s0, s1, rect) is not None
                       for s0, s1 in segments):
                    continue
                centre = ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)
                clipped = _segment_rect_clip(anchor, centre, rect, margin=0.0)
                t = clipped[0] if clipped is not None else 1.0
                end = (anchor[0] + t * (centre[0] - anchor[0]),
                       anchor[1] + t * (centre[1] - anchor[1]))
                if any(_segment_rect_clip(anchor, end, o) is not None
                       for o in obstacles):
                    continue
                if not allow_crossing and any(
                    _segments_cross(anchor, end, s0, s1) for s0, s1 in segments
                ):
                    continue
                chosen = (cost, xoff, yoff, rect, (anchor, end))
                break
            if chosen is not None:
                break

        if chosen is not None:
            cost, xoff, yoff, rect, seg = chosen
            obstacles.append(rect)
            segments.append(seg)
            plan[i] = dict(mode="leader", xoff=xoff, yoff=yoff, anchor=anchor,
                           rect=rect, cost=cost)
            continue

        # Nowhere for a leader line. Put the box beside the marker with the line
        # switched off, which says less but says nothing false.
        direct = None
        for xoff, yoff in _CALLOUT_DIRECT_OFFSETS_PT:
            rect = rect_for(xoff, yoff)
            if not inside(rect):
                continue
            if any(_rects_overlap(rect, o) for o in obstacles):
                continue
            if any(_segment_rect_clip(s0, s1, rect) is not None
                   for s0, s1 in segments):
                continue
            direct = (xoff, yoff, rect)
            break
        if direct is not None:
            xoff, yoff, rect = direct
            obstacles.append(rect)
            plan[i] = dict(mode="direct", xoff=xoff, yoff=yoff, anchor=anchor,
                           rect=rect, cost=0.0)
            continue

        plan[i] = dict(mode="dropped", anchor=anchor, cost=0.0)
    return plan


def _plan_score(plan):
    """Rank plans: most leader lines first, then fewest dropped, then tightest."""
    leaders = sum(1 for d in plan.values() if d["mode"] == "leader")
    dropped = sum(1 for d in plan.values() if d["mode"] == "dropped")
    cost = sum(d["cost"] for d in plan.values())
    return (leaders, -dropped, -cost)


def _place_spike_callouts(fig, ax, callout_artists, other_axes=()):
    """Put each callout where neither its box nor its leader line crosses any
    other text on the chart.

    Placement is measured, not assumed. Every other artist has been drawn and
    the layout is final by the time this runs, so the real bounding boxes of the
    clip line label, the daily minimum and maximum labels, the horizon notice,
    the market notice band labels and the legend are all known. Each callout
    tries a fan of offsets in order of increasing distance from its marker and
    takes the first that sits inside the axes and clear of everything, including
    the callouts placed before it.

    Which callout picks first changes what is left for the others, so a few
    orders are costed and the one that lands the most leader lines wins. A
    callout with nowhere to go loses its line and sits beside its own marker,
    and if even that collides it is dropped and the marker triangle speaks for
    itself. Drawing over another label is never an option.

    Returns a list of (label, mode) with mode one of "leader", "direct" or
    "dropped", for the tests and for anyone reading a log.
    """
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    axes_list = [ax] + [a for a in other_axes if a is not None]
    fixed_rects = text_obstacle_rects(axes_list, renderer, exclude=callout_artists)
    ax_rect = _as_rect(ax.get_window_extent(renderer))
    scale = fig.dpi / 72.0

    metrics = {}
    for i, art in enumerate(callout_artists):
        box = callout_box_rect(art, renderer)
        if box is None:
            continue
        x_data = ax.xaxis.convert_units(art.xy[0])
        y_data = ax.yaxis.convert_units(art.xy[1])
        anchor = tuple(ax.transData.transform((x_data, y_data)))
        ox, oy = art.xyann
        metrics[i] = dict(
            anchor=anchor,
            width=box[2] - box[0],
            height=box[3] - box[1],
            # Gap between the offset point and the painted box border, which is
            # the bbox pad. Measured rather than recomputed from the pad setting
            # so it stays right if the box style changes.
            pad_x=(anchor[0] + ox * scale) - box[0],
            pad_y=(anchor[1] + oy * scale) - box[1],
        )

    indices = sorted(metrics)
    orders = []
    if indices:
        by_x = sorted(indices, key=lambda i: metrics[i]["anchor"][0])
        orders = [indices, list(reversed(indices)), by_x, list(reversed(by_x))]
    best = None
    for order in orders:
        for corridor in (False, True):
            plan = _plan_callout_placements(order, metrics, fixed_rects, ax_rect,
                                            scale, corridor=corridor)
            score = _plan_score(plan)
            if best is None or score > best[0]:
                best = (score, plan)
    plan = best[1] if best is not None else {}

    report = []
    for i, art in enumerate(callout_artists):
        decision = plan.get(i)
        label = str(art.get_text())
        if decision is None or decision["mode"] == "dropped":
            report.append((label, "dropped"))
            art.remove()
            continue
        anchor = decision["anchor"]
        if anchor != metrics[i]["anchor"]:
            art.xy = tuple(ax.transData.inverted().transform(anchor))
        art.set_ha('left' if decision["xoff"] >= 0 else 'right')
        art.xyann = (decision["xoff"], decision["yoff"])
        if decision["mode"] == "direct" and getattr(art, "arrow_patch", None) is not None:
            art.arrow_patch.set_visible(False)
        report.append((label, decision["mode"]))
    return report



# ── Issue #93: measured placement for the non callout labels ─────────────────


def _text_only_rect(art, renderer):
    """The rectangle of the glyphs alone, ignoring any box or arrow.

    Text.get_window_extent is taken unbound on purpose. An Annotation reports an
    extent that swallows its own arrow, and a label with a bbox patch reports
    the patch, so neither is the box the alignment maths below works in.
    """
    import matplotlib.text as mtext
    return _as_rect(mtext.Text.get_window_extent(art, renderer))


def _painted_pad(art, renderer, text_rect):
    """How far the painted box extends past the glyphs, per side.

    The day divider labels carry a white background patch, and that patch is
    what the reader sees covering whatever is under it, so it is the patch that
    has to be kept clear of other text. Measured rather than recomputed from the
    pad setting, so it stays right if the style changes.
    """
    patch = art.get_bbox_patch() if hasattr(art, "get_bbox_patch") else None
    if patch is None:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        box = _as_rect(patch.get_window_extent(renderer))
    except (RuntimeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (max(0.0, text_rect[0] - box[0]), max(0.0, text_rect[1] - box[1]),
            max(0.0, box[2] - text_rect[2]), max(0.0, box[3] - text_rect[3]))


def _aligned_rect(point, width, height, ha, va, pad):
    """The painted rectangle of a label of this size aligned at this point."""
    if ha == "left":
        x0 = point[0]
    elif ha == "right":
        x0 = point[0] - width
    else:
        x0 = point[0] - width / 2.0
    if va == "bottom":
        y0 = point[1]
    elif va == "top":
        y0 = point[1] - height
    else:
        y0 = point[1] - height / 2.0
    return (x0 - pad[0], y0 - pad[1], x0 + width + pad[2], y0 + height + pad[3])


def _extreme_label_candidates(above: bool, base_dy: float):
    """Offsets to try for a daily extreme label, nearest first.

    Three families, ordered by the cost weights above: further out on the side
    the label has always been drawn, then the far side of the marker, then beside
    it at its own height. A maximum labelled underneath itself sits on the price
    curve, which is not lovely, but it is in its own column and beats both
    printing over the legend and drifting toward the next day's dot.
    """
    va = "bottom" if above else "top"
    flip_va = "top" if above else "bottom"
    step_sign = 1.0 if above else -1.0
    out = []
    for yi, ystep in enumerate(_LABEL_Y_STEPS_PT):
        dy = base_dy + step_sign * ystep
        for xi, mag in enumerate(_LABEL_X_OFFSETS_PT):
            for si, sign in enumerate((1, -1) if mag else (1,)):
                out.append((_LABEL_COST_X_WEIGHT * xi + yi + 0.05 * si,
                            sign * mag, dy, "center", va))
    for yi, ystep in enumerate(_LABEL_Y_STEPS_PT):
        dy = -step_sign * (abs(base_dy) + ystep)
        for xi, mag in enumerate(_LABEL_X_OFFSETS_PT):
            for si, sign in enumerate((1, -1) if mag else (1,)):
                out.append((_LABEL_COST_FLIP + _LABEL_COST_X_WEIGHT * xi
                            + yi + 0.05 * si, sign * mag, dy, "center", flip_va))
    for xi, mag in enumerate(_LABEL_SIDE_OFFSETS_PT):
        for si, sign in enumerate((1, -1)):
            out.append((_LABEL_COST_SIDE + _LABEL_COST_SIDE_X_WEIGHT * xi
                        + 0.05 * si, sign * mag, 0.0,
                        "left" if sign > 0 else "right", "center"))
    out.sort(key=lambda c: c[0])
    return out


def _divider_label_candidates():
    """Offsets to try for a day divider label, nearest first.

    Sideways first and on the right of its midnight line first, because that is
    where it has always been and the label names the day that starts there. The
    left of the line is next, which still reads correctly since the line is the
    boundary. Upward tiers come last: they lift the label off the x axis tick
    labels at the cost of putting it over the price band, and the white patch
    behind it makes that legible.
    """
    out = []
    for yi, ystep in enumerate(_DIVIDER_Y_STEPS_PT):
        for xi, mag in enumerate(_DIVIDER_X_OFFSETS_PT):
            for si, (sign, ha) in enumerate(((1, "left"), (-1, "right"))):
                out.append((xi + 0.05 * si + 2.0 * yi, sign * mag,
                            float(ystep), ha, "top"))
    out.sort(key=lambda c: c[0])
    return out


def _zone_label_candidates(width_pt: float):
    """Offsets to try for the confidence boundary label, nearest first.

    Down its own line first, since sliding down keeps the label centred on the
    boundary and that is what makes "reliable" and "uncertain" point at the right
    halves of the chart. Sideways is allowed only while the boundary line is
    still inside the label, which is why the magnitudes are filtered against the
    label's measured half width.
    """
    limit = width_pt / 2.0 - 4.0
    out = []
    for yi, dy in enumerate(_ZONE_LABEL_Y_STEPS_PT):
        for xi, mag in enumerate(_ZONE_LABEL_X_OFFSETS_PT):
            if mag > limit:
                continue
            for si, sign in enumerate((1, -1) if mag else (1,)):
                out.append((yi + 1.5 * xi + 0.05 * si, sign * mag, float(dy),
                            "center", "top"))
    out.sort(key=lambda c: c[0])
    return out


def _overlap_area(a, b, margin: float = _CALLOUT_COLLISION_MARGIN_PX) -> float:
    """Area of the overlap of two rectangles, inflated by the margin."""
    w = min(a[2], b[2]) - max(a[0], b[0]) + 2 * margin
    h = min(a[3], b[3]) - max(a[1], b[1]) + 2 * margin
    if w <= 0.0 or h <= 0.0:
        return 0.0
    return w * h


def _plan_label_placements(metrics, order, fixed_rects, ax_rect, scale):
    """Choose an offset for each movable label. Pure geometry, nothing moves.

    A label takes the first candidate in its own fan that is inside the axes and
    clear of every fixed rectangle and of every label already placed. Inside the
    axes is a hard requirement and is what stops a label crossing the right
    spine into the $/MWh tick label gutter in the first place.

    When no candidate in the fan is free the label degrades, and how it degrades
    depends on what the label is:

    * a daily extreme is dropped, because its marker dot stays on the chart and
      the legend says what the dot means, so the reader loses a number and is
      told nothing false. This is the same choice PR #90 made for a callout with
      nowhere to go;
    * a day divider label and the confidence boundary label are kept at their
      least overlapping offset, because there is no marker standing in for them.
      Dropping a day divider label would leave a dashed line the reader cannot
      date, and the overlap that remains is a few pixels against a tick label or
      the legend frame rather than across a line of text.

    Returns {index: decision}.
    """
    obstacles = list(fixed_rects)
    plan = {}
    for i in order:
        m = metrics[i]
        anchor, width, height, pad = m["anchor"], m["width"], m["height"], m["pad"]

        def rect_for(dx, dy, ha, va):
            point = (anchor[0] + dx * scale, anchor[1] + dy * scale)
            return _aligned_rect(point, width, height, ha, va, pad)

        def inside(rect):
            return (ax_rect[0] <= rect[0] and rect[2] <= ax_rect[2]
                    and ax_rect[1] <= rect[1] and rect[3] <= ax_rect[3])

        # First pass only asks whether a candidate is free, so it stops at the
        # first obstacle it touches. Almost every label is free at its first
        # candidate, which is the offset it has always been drawn at, so this
        # pass costs about one overlap test per label per obstacle.
        best = None
        for cost, dx, dy, ha, va in m["candidates"]:
            rect = rect_for(dx, dy, ha, va)
            if not inside(rect):
                continue
            if any(_overlap_area(rect, o) > 0.0 for o in obstacles):
                continue
            best = (cost, dx, dy, ha, va, rect)
            break
        if best is not None:
            cost, dx, dy, ha, va, rect = best
            obstacles.append(rect)
            plan[i] = dict(mode="clear", dx=dx, dy=dy, ha=ha, va=va, rect=rect,
                           cost=cost, overlap=0.0)
            continue

        if m["droppable"]:
            plan[i] = dict(mode="dropped", dx=None, dy=None, ha=None, va=None,
                           rect=None, cost=0.0, overlap=0.0)
            continue

        # Second pass, only for a label with nowhere free, so the full overlap
        # area is worth measuring.
        fallback = None
        for cost, dx, dy, ha, va in m["candidates"]:
            rect = rect_for(dx, dy, ha, va)
            if not inside(rect):
                continue
            area = sum(_overlap_area(rect, o) for o in obstacles)
            if fallback is None or (area, cost) < (fallback[0], fallback[1]):
                fallback = (area, cost, dx, dy, ha, va, rect)
        if fallback is not None:
            area, cost, dx, dy, ha, va, rect = fallback
            obstacles.append(rect)
            plan[i] = dict(mode="least_overlap", dx=dx, dy=dy, ha=ha, va=va,
                           rect=rect, cost=cost, overlap=area)
            continue
        # Not one candidate fits inside the axes, which takes a chart barely
        # wider than a single label. Leave the label exactly where it has always
        # been drawn and say so, rather than inventing a position.
        cost, dx, dy, ha, va = m["candidates"][0]
        plan[i] = dict(mode="default", dx=dx, dy=dy, ha=ha, va=va, rect=None,
                       cost=cost, overlap=float("inf"))
    return plan


def _label_plan_score(plan):
    """Rank plans: fewest degraded, then least overlap, then tightest."""
    degraded = sum(1 for d in plan.values() if d["mode"] != "clear")
    # A dropped label is the worst outcome available, so a plan that drops one
    # loses to any plan that does not, even a tighter one.
    degraded += sum(1 for d in plan.values() if d["mode"] == "dropped")
    overlap = sum(d["overlap"] for d in plan.values() if d["rect"] is not None)
    cost = sum(d["cost"] for d in plan.values())
    return (-degraded, -overlap, -cost)


def place_movable_labels(fig, ax, movable, other_axes=()):
    """Move each daily extreme, day divider and boundary label clear of the rest.

    ``movable`` is a list of (kind, artist) with kind one of "max", "min",
    "divider" or "zone". Every artist must be an annotation positioned in offset
    points, because an offset is the only thing this changes: the anchor stays on
    the marker or on the midnight line it belongs to.

    This runs after every other artist is drawn and after tight_layout, so the
    tick labels of both axes, the legend frame, the clip line label, the grid
    stress labels and the title are all measurable, and it runs before the spike
    callouts are created so the callouts route around where these labels finally
    ended up rather than where they started.

    Returns a list of (label, mode) with mode one of "clear", "dropped",
    "least_overlap" or "default", for the tests and for anyone reading a log.
    """
    if not movable:
        return []
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    axes_list = [ax] + [a for a in other_axes if a is not None]
    artists = [art for _kind, art in movable]
    fixed_rects = text_obstacle_rects(axes_list, renderer, exclude=artists)
    ax_rect = _as_rect(ax.get_window_extent(renderer))
    scale = fig.dpi / 72.0

    metrics = {}
    for i, (kind, art) in enumerate(movable):
        try:
            text_rect = _text_only_rect(art, renderer)
        except (RuntimeError, ValueError, AttributeError):
            continue
        if text_rect[2] - text_rect[0] <= 0 or text_rect[3] - text_rect[1] <= 0:
            continue
        x_data = ax.xaxis.convert_units(art.xy[0])
        y_data = ax.yaxis.convert_units(art.xy[1])
        base_dy = art.xyann[1]
        if kind == "max":
            candidates = _extreme_label_candidates(True, base_dy)
        elif kind == "min":
            candidates = _extreme_label_candidates(False, base_dy)
        elif kind == "divider":
            candidates = _divider_label_candidates()
        else:
            candidates = _zone_label_candidates(
                (text_rect[2] - text_rect[0]) * 72.0 / fig.dpi)
        metrics[i] = dict(
            anchor=tuple(ax.transData.transform((x_data, y_data))),
            width=text_rect[2] - text_rect[0],
            height=text_rect[3] - text_rect[1],
            pad=_painted_pad(art, renderer, text_rect),
            candidates=candidates,
            droppable=kind in ("max", "min"),
        )

    # Order matters: whoever picks first gets the room. The day dividers go
    # first because their anchor is a line the reader is looking at and they can
    # only move a few points sideways, then the extremes, which can go anywhere
    # around their own dot. A couple of orders within the extremes are costed and
    # the best kept, the same way the callout orders are, because on a crowded
    # chart the label that picks first decides what is left for the others.
    dividers = [i for i in metrics if movable[i][0] == "divider"]
    zones = [i for i in metrics if movable[i][0] == "zone"]
    extremes = [i for i in metrics if movable[i][0] in ("max", "min")]
    by_x = sorted(extremes, key=lambda i: metrics[i]["anchor"][0])
    orders = [
        zones + dividers + by_x,
        zones + dividers + list(reversed(by_x)),
        zones + dividers + sorted(extremes,
                                  key=lambda i: -metrics[i]["anchor"][1]),
    ]
    best = None
    for order in orders:
        plan = _plan_label_placements(metrics, order, fixed_rects, ax_rect, scale)
        score = _label_plan_score(plan)
        if best is None or score > best[0]:
            best = (score, plan)
    plan = best[1] if best is not None else {}

    report = []
    for i, (_kind, art) in enumerate(movable):
        decision = plan.get(i)
        if decision is None:
            report.append((str(art.get_text()), "default"))
            continue
        if decision["mode"] == "dropped":
            report.append((str(art.get_text()), "dropped"))
            art.remove()
            continue
        art.set_ha(decision["ha"])
        art.set_va(decision["va"])
        art.xyann = (decision["dx"], decision["dy"])
        report.append((str(art.get_text()), decision["mode"]))
    return report


# ── Issue #93: the general collision measurement, no chosen subset ───────────


def all_text_items(fig, renderer):
    """Every visible piece of text on the whole figure, as (kind, text, rect).

    Deliberately not a subset. Two sweeps written for PR #90 were each accurate
    inside their own scope and silent about a daily extreme label sitting across
    the legend, because one scored callouts against callouts and the other
    scored the strip above the clip line. This one takes both axes' tick labels
    and axis labels, the title, every annotation and free text, and the legend
    frame, on every axes of the figure.

    The legend is measured as its frame. Its frame is opaque but the labels that
    land on it carry a higher zorder, so they are painted over the legend text
    and the reader really does see two strings in the same pixels.
    """
    out = []

    def add(kind, art):
        if art is None or not art.get_visible():
            return
        if not str(art.get_text()).strip():
            return
        patch = art.get_bbox_patch() if hasattr(art, "get_bbox_patch") else None
        try:
            if patch is not None:
                rect = _as_rect(patch.get_window_extent(renderer))
            else:
                rect = _text_only_rect(art, renderer)
        except (RuntimeError, ValueError, AttributeError):
            return
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return
        out.append((kind, str(art.get_text()), rect))

    for ai, axes in enumerate(fig.axes):
        for art in axes.texts:
            add("label%d" % ai, art)
        add("title%d" % ai, axes.title)
        # A twinx axes has its whole x axis switched off by matplotlib while the
        # individual tick label artists still report themselves visible, so
        # without the axis level check every x tick label is reported as sitting
        # on an invisible duplicate of itself. The check is on the axis and
        # nothing else; no artist is skipped for being inconvenient.
        if axes.xaxis.get_visible():
            for art in axes.get_xticklabels():
                add("xtick%d" % ai, art)
            add("xlabel%d" % ai, axes.xaxis.label)
        if axes.yaxis.get_visible():
            for art in axes.get_yticklabels():
                add("ytick%d" % ai, art)
            add("ylabel%d" % ai, axes.yaxis.label)
        legend = axes.get_legend()
        if legend is not None and legend.get_visible():
            try:
                out.append(("legend%d" % ai, "<legend frame>",
                            _as_rect(legend.get_window_extent(renderer))))
            except (RuntimeError, ValueError):
                pass
    return out


def text_collision_pairs(fig, min_overlap_px: float = 0.5):
    """Every pair of text things on the figure whose boxes overlap.

    Returns a list of (kind_a, text_a, kind_b, text_b, overlap_x, overlap_y).
    """
    fig.canvas.draw()
    items = all_text_items(fig, fig.canvas.get_renderer())
    hits = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            ki, ti, a = items[i]
            kj, tj, b = items[j]
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox > min_overlap_px and oy > min_overlap_px:
                hits.append((ki, ti, kj, tj, round(ox, 2), round(oy, 2)))
    return hits


def render_forecast_chart(forecast_data: list, region: str, annotations: list | None = None) -> bytes:
    """Render the 7-day forecast chart. Returns PNG bytes."""
    import datetime
    from collections import defaultdict

    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib.figure as mplfig
        import matplotlib.patches as mpatches
        import matplotlib.ticker as ticker
        import numpy as np
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError:
        return _placeholder_png(
            "Forecast chart unavailable\n(matplotlib not installed)"
        )

    NEM_TZ = datetime.timezone(datetime.timedelta(hours=10))

    if not forecast_data:
        return b""

    times, raws, cals, p10s, p90s, sources = [], [], [], [], [], []
    horizons = []
    spike_first_runs = []
    spike_credibles = []
    forecast_run_at = None
    for p in forecast_data:
        try:
            dt = datetime.datetime.fromisoformat(p['nemtime'])
            times.append(dt)
            raws.append(float(p.get('raw_value') or 0))
            cal_val = float(p.get('calibrated') or p.get('value') or 0)
            cals.append(cal_val)
            p10s.append(float(p.get('p10') if p.get('p10') is not None else cal_val))
            p90s.append(float(p.get('p90') if p.get('p90') is not None else cal_val))
            sources.append(p.get('calibrated_source', 'ols'))
            horizons.append(float(p.get('horizon_hours', 0)))
            spike_first_runs.append(p.get('spike_first_run', True))
            spike_credibles.append(p.get('spike_credible'))
            if forecast_run_at is None and p.get('forecast_run_at'):
                try:
                    forecast_run_at = datetime.datetime.fromisoformat(p['forecast_run_at'])
                except (ValueError, TypeError):
                    pass
        except (KeyError, ValueError):
            continue

    if not times:
        return b""

    times = np.array(times)
    raws  = np.array(raws)
    cals  = np.array(cals)
    p10s  = np.array(p10s)
    p90s  = np.array(p90s)

    # Dynamic clip: 99th percentile of calibrated values + 15% headroom, min 0.15
    # Exclude spike-raw intervals from CLIP_Y to avoid compressing the chart
    non_spike_mask = np.array([r < _SPIKE_CALLOUT_THRESHOLD_48H for r in raws])
    non_spike_cals = cals[non_spike_mask] if non_spike_mask.any() else cals
    p99 = float(np.percentile(non_spike_cals, 99)) if len(non_spike_cals) > 0 else 0.20
    CLIP_Y = float(np.ceil(max(p99 * 1.15, 0.15) / 0.05) * 0.05)

    # ── Rec 1 + 4: classify spike callout intervals ─────────────────────────
    # Done here rather than at the drawing site because the y limits below have
    # to know whether any callout boxes will be drawn before they are set.
    # spike_credible is a tri-state: True, None when a covariate the gate needs
    # is missing, and absent below SPIKE_THRESHOLD. Only True is a confirmed
    # credible spike, so the test is "is not True" and a missing covariate
    # draws nothing rather than being read as a negative answer.
    confirmed_indices = []
    candidate_indices = []
    for i in range(len(raws)):
        if float(raws[i]) < _SPIKE_CALLOUT_THRESHOLD_48H:
            continue
        if spike_credibles[i] is not True:
            continue
        eligible, style = _is_spike_callout_eligible(
            float(raws[i]), horizons[i], spike_first_runs[i],
        )
        if not eligible:
            continue
        if style == "confirmed":
            confirmed_indices.append(i)
        elif style == "candidate":
            candidate_indices.append(i)

    # Y limits, fixed once here so the zone label, the callout tiers and
    # set_ylim below all work in the same span. Callout boxes are placed a
    # fixed number of points above the clip line, so when there are any the
    # axis top is raised until that many points fit inside the axes.
    y_min = min(float(np.min(p10s)), -0.04)
    y_bottom = y_min * 1.25
    y_top = CLIP_Y * 1.35
    if confirmed_indices:
        y_top = max(
            y_top,
            (CLIP_Y - _CALLOUT_HEADROOM_FRAC * y_bottom)
            / (1.0 - _CALLOUT_HEADROOM_FRAC),
        )

    # Per-day min/max on calibrated values (all intervals — isotonic values
    # are clean normal-market estimates even for spike-raw inputs)
    by_day = defaultdict(list)
    for i, t in enumerate(times):
        by_day[t.strftime('%Y-%m-%d')].append((i, float(cals[i])))
    day_extremes = {}
    for day, pts in by_day.items():
        day_extremes[day] = {
            'min': min(pts, key=lambda x: x[1]),
            'max': max(pts, key=lambda x: x[1]),
        }

    # Labels whose position is decided by measurement at the end of this
    # function, as (kind, artist). See place_movable_labels.
    movable_labels: list[tuple] = []

    fig = mplfig.Figure(figsize=(15, 6), facecolor='white')
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_facecolor('white')

    # ── Rec 5: Compute confidence zone boundaries ────────────────────────────
    # Zones relative to forecast_run_at:
    #   Zone A: 0-24h  — solid line, full band opacity
    #   Zone B: 24-72h — faded line, reduced band opacity
    #   Zone C: 72h+   — dotted line, very low band opacity
    zone_24h = None
    zone_72h = None
    if forecast_run_at is not None:
        zone_24h = forecast_run_at + datetime.timedelta(hours=24)
        zone_72h = forecast_run_at + datetime.timedelta(hours=72)

    # ── Grid stress annotations ──────────────────────────────────────────────
    notice_types_present: set[tuple] = set()
    if annotations:
        NOTICE_COLORS = {
            ("LOR", 1): ("#F39C12", 0.15, "LOR1"),   # amber
            ("LOR", 2): ("#E67E22", 0.20, "LOR2"),   # orange
            ("LOR", 3): ("#C0392B", 0.30, "LOR3"),   # red
            ("MSL", 1): ("#8E44AD", 0.15, "MSL1"),   # purple
            ("MSL", 2): ("#7D3C98", 0.22, "MSL2"),
            ("MSL", 3): ("#6C3483", 0.30, "MSL3"),
        }
        # Track label positions to stagger vertically when notices overlap in time
        # key: notice_id or index, value: y offset tier (0, 1, 2...)
        # Point offsets above the clip line, not fractions of it, so the tiers
        # keep their spacing when the callout headroom raises the axis top.
        # Ordered highest first, as before, so a lone notice sits at the top of
        # the notice strip.
        label_y_levels = list(reversed(_NOTICE_LABEL_OFFSETS_PT))
        # Group placed labels by approximate x-position bucket (6h windows)
        # to detect collisions and assign vertical tiers
        placed: list[tuple] = []  # (mid_num, tier)
        for ann in annotations:
            if ann.is_cancelled:
                continue
            color_info = NOTICE_COLORS.get((ann.notice_type, ann.level))
            if not color_info:
                continue
            color, alpha, label_text = color_info
            notice_types_present.add((ann.notice_type, ann.level))
            ax.axvspan(
                ann.period_from, ann.period_to,
                alpha=alpha, color=color, zorder=1, linewidth=0
            )
            mid = ann.period_from + (ann.period_to - ann.period_from) / 2
            mid_num = mdates.date2num(mid)
            # Assign vertical tier: find lowest tier not already used within
            # a 6-hour window of this label's mid-point
            tier = 0
            for _ in range(len(label_y_levels)):
                collision = any(
                    abs(mid_num - px) < 0.25 and pt == tier
                    for px, pt in placed
                )
                if not collision:
                    break
                tier += 1
            tier = min(tier, len(label_y_levels) - 1)
            placed.append((mid_num, tier))
            ax.annotate(
                label_text, xy=(mid, CLIP_Y),
                xytext=(0, label_y_levels[tier]), textcoords="offset points",
                ha="center", va="bottom", fontsize=7, color=color,
                fontweight="bold", zorder=5,
            )

    # ── Rec 5: Confidence-tiered p10/p90 band and forecast lines ─────────────
    # Split data into three zones and render each with different styling.
    if zone_24h is not None:
        zone_a = np.array([t < zone_24h for t in times])
        zone_b = np.array([zone_24h <= t < zone_72h for t in times])
        zone_c = np.array([t >= zone_72h for t in times])

        # Extend each zone by 1 point at the boundary for seamless joins
        for z_prev, z_next in [(zone_a, zone_b), (zone_b, zone_c)]:
            idx_prev = np.where(z_prev)[0]
            idx_next = np.where(z_next)[0]
            if len(idx_prev) > 0 and len(idx_next) > 0:
                z_next[idx_prev[-1]] = True  # overlap last point of prev zone

        # Zone A (0-24h): solid line, full band
        if zone_a.any():
            ax.fill_between(times[zone_a],
                            np.clip(p10s[zone_a], None, CLIP_Y),
                            np.clip(p90s[zone_a], None, CLIP_Y),
                            color='#BBDEFB', alpha=0.45, zorder=2)
            ax.plot(times[zone_a], np.clip(cals[zone_a], None, CLIP_Y),
                    color='#1565C0', linewidth=2.0, zorder=4)
            ax.plot(times[zone_a], np.clip(raws[zone_a], None, CLIP_Y),
                    color='#888888', linewidth=1.0, alpha=0.7, linestyle='--', zorder=5)

        # Zone B (24-72h): faded line, reduced band
        if zone_b.any():
            ax.fill_between(times[zone_b],
                            np.clip(p10s[zone_b], None, CLIP_Y),
                            np.clip(p90s[zone_b], None, CLIP_Y),
                            color='#BBDEFB', alpha=0.12, zorder=2,
                            hatch='///', edgecolor='#90CAF9', linewidth=0.0)
            ax.plot(times[zone_b], np.clip(cals[zone_b], None, CLIP_Y),
                    color='#1565C0', linewidth=1.6, alpha=0.65, zorder=4)
            ax.plot(times[zone_b], np.clip(raws[zone_b], None, CLIP_Y),
                    color='#888888', linewidth=0.8, alpha=0.5, linestyle='--', zorder=5)

        # Zone C (72h+): dotted line, very low band
        if zone_c.any():
            ax.fill_between(times[zone_c],
                            np.clip(p10s[zone_c], None, CLIP_Y),
                            np.clip(p90s[zone_c], None, CLIP_Y),
                            color='#BBDEFB', alpha=0.06, zorder=2)
            ax.plot(times[zone_c], np.clip(cals[zone_c], None, CLIP_Y),
                    color='#1565C0', linewidth=1.2, alpha=0.45, linestyle=':', zorder=4)
            ax.plot(times[zone_c], np.clip(raws[zone_c], None, CLIP_Y),
                    color='#888888', linewidth=0.7, alpha=0.35, linestyle=':', zorder=5)

        # 24h and 72h boundary lines
        if times[0] < zone_24h < times[-1]:
            ax.axvline(zone_24h, color='#666688', linewidth=0.8,
                       linestyle='--', alpha=0.3, zorder=3)
            # An annotation at a zero offset rather than a plain text, so the
            # placement pass below can slide it down its own line when the
            # legend or the clip line label is already in that spot (#93).
            movable_labels.append(('zone', ax.annotate(
                '\u2190 reliable | uncertain \u2192',
                xy=(zone_24h, y_top * 0.96), xytext=(0, 0),
                textcoords='offset points',
                fontsize=6.5, color='#666688', alpha=0.6,
                ha='center', va='top', zorder=8)))
        if times[0] < zone_72h < times[-1]:
            ax.axvline(zone_72h, color='#666688', linewidth=0.8,
                       linestyle='--', alpha=0.3, zorder=3)
    else:
        # No forecast_run_at: render uniformly (legacy path)
        ax.fill_between(times, np.clip(p10s, None, CLIP_Y), np.clip(p90s, None, CLIP_Y),
                        color='#BBDEFB', alpha=0.45, zorder=2)
        ax.plot(times, np.clip(cals, None, CLIP_Y),
                color='#1565C0', linewidth=2.0, label='Calibrated', zorder=4)
        ax.plot(times, np.clip(raws, None, CLIP_Y),
                color='#888888', linewidth=1.0, alpha=0.7, linestyle='--',
                label='PD7day Raw', zorder=5)

    # Per-day min/max labels. Created at the offset they have always used, which
    # is also the first candidate the placement below tries, and collected so
    # that placement can move the ones that would otherwise land on the legend,
    # on a tick label or on each other (issue #93).
    for day, ex in sorted(day_extremes.items()):
        mi, mv = ex['max']
        mt = times[mi]
        mv_plot = min(mv, CLIP_Y)
        ax.scatter([mt], [mv_plot], color='#C62828', s=30, zorder=7, marker='o', linewidths=0)
        movable_labels.append(('max', ax.annotate(
            f'${mv:.3f}', xy=(mt, mv_plot),
            xytext=(0, 9), textcoords='offset points',
            fontsize=7.2, color='#B71C1C', ha='center', va='bottom',
            fontweight='bold', zorder=9)))

        ni, nv = ex['min']
        nt = times[ni]
        ax.scatter([nt], [nv], color='#1B5E20', s=30, zorder=7, marker='o', linewidths=0)
        movable_labels.append(('min', ax.annotate(
            f'${nv:.3f}', xy=(nt, nv),
            xytext=(0, -11), textcoords='offset points',
            fontsize=7.2, color='#1B5E20', ha='center', va='top',
            fontweight='bold', zorder=9)))

    # ── Rec 1 + 4: Horizon-gated spike callouts with persistence styling ─────
    # confirmed_indices and candidate_indices were classified above, next to
    # the y limits that depend on them.

    # Confirmed spike markers — solid red triangle (existing style)
    if confirmed_indices:
        ct = [times[i] for i in confirmed_indices]
        ax.scatter(ct, [CLIP_Y * 0.96] * len(ct),
                   color='#C62828', marker='^', s=55, zorder=6)

    # Candidate spike markers — light grey triangle (first-run, unconfirmed)
    if candidate_indices:
        ct = [times[i] for i in candidate_indices]
        ax.scatter(ct, [CLIP_Y * 0.96] * len(ct),
                   color='#AAAAAA', marker='^', s=35, zorder=6, alpha=0.6)

    # Build callout clusters from confirmed spikes only. Only the cluster peaks
    # are worked out here. The labels themselves are created and placed at the
    # very end of this function, once every other artist exists and the layout is
    # final, because where a leader line can go depends on where the other text
    # actually landed, and that is only knowable by measurement.
    callout_peaks: list[tuple] = []
    pt_indices = confirmed_indices
    if pt_indices:
        clusters: list[list[int]] = []
        current: list[int] = [pt_indices[0]]
        for prev_idx, idx in zip(pt_indices, pt_indices[1:]):
            gap = (times[idx] - times[prev_idx]).total_seconds() / 60
            if gap <= _CALLOUT_CLUSTER_GAP_MIN:
                current.append(idx)
            else:
                clusters.append(current)
                current = [idx]
        clusters.append(current)

        for cluster in clusters:
            # The peak of the cluster is its highest raw forecast, because the
            # raw forecast is what a spike callout is about. The label used to
            # report the calibrated value, so a $12.00/kWh raw spike was
            # annotated "$0.18/kWh", which is the number the calibrated line
            # already draws and says nothing about the spike being called out.
            c_raws = [float(raws[i]) for i in cluster]
            max_raw = max(c_raws)
            peak_time = [times[i] for i in cluster][c_raws.index(max_raw)]
            callout_peaks.append((max_raw, peak_time))
        # Biggest spike first, so on a crowded chart the most important label
        # gets first pick of the free space.
        callout_peaks.sort(key=lambda kv: -kv[0])

    # Grid
    ax.yaxis.grid(True, color='#DDDDDD', linewidth=0.5, alpha=0.7, zorder=1)
    ax.xaxis.grid(True, color='#EEEEEE', linewidth=0.4, alpha=0.5, zorder=1)
    ax.set_axisbelow(True)

    # X-axis: minor ticks every 6h (grid only), labelled ticks at 06:00 and 18:00 only
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[6, 18], tz=NEM_TZ))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=NEM_TZ))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[0, 12], tz=NEM_TZ))
    ax.xaxis.grid(True, which='minor', color='#EEEEEE', linewidth=0.4, alpha=0.5, zorder=1)
    ax.tick_params(axis='x', labelsize=8.5, pad=2)

    for mt in [t for t in times if t.hour == 0 and t.minute == 0]:
        ax.axvline(mdates.date2num(mt), color='#CCCCCC', linewidth=1.0,
                   linestyle='--', zorder=1)
        # Offset in points, not in date units. The old +0.02 was 29 minutes of
        # chart, which is 4 px on a seven day chart and a third of a day on a
        # twelve interval one, and the label for the last midnight ran past the
        # right spine into the $/MWh tick labels because nothing clamped it (#93).
        movable_labels.append(('divider', ax.annotate(
            mt.strftime('%a %-d %b'),
            xy=(mdates.date2num(mt), y_min * 0.97),
            xytext=(_DIVIDER_LABEL_OFFSET_PT, 0), textcoords='offset points',
            fontsize=9, color='#333333', va='top', ha='left', zorder=8,
            bbox=dict(facecolor='white', edgecolor='none', alpha=0.7, pad=1))))

    # Y-axis left: $/kWh
    ax.set_ylim(bottom=y_bottom, top=y_top)
    ax.set_ylabel('$/kWh', fontsize=10, labelpad=6)
    ax.yaxis.set_tick_params(labelsize=9)
    ax.axhline(CLIP_Y, color='#C62828', linewidth=0.8, linestyle=':', alpha=0.6)
    ax.annotate(f'clip: p99+15% = ${CLIP_Y:.2f}/kWh',
                xy=(times[min(2, len(times) - 1)], CLIP_Y),
                xytext=(0, _CLIP_LABEL_OFFSET_PT), textcoords='offset points',
                fontsize=7, color='#C62828', ha='left', va='bottom',
                alpha=0.85)

    # Y-axis right: $/MWh — use twinx on the figure's ax (OO API, thread-safe)
    ax2 = ax.twinx()
    ax2.set_ylim(ax.get_ylim()[0] * 1000, ax.get_ylim()[1] * 1000)
    ax2.set_ylabel('$/MWh', fontsize=10, labelpad=8)
    ax2.yaxis.set_tick_params(labelsize=9, pad=4)
    ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f'{x:.0f}'))

    # Title
    ax.set_title(f'NEM PD7DAY {region} \u2014 7-Day Pre-Dispatch Spot Price Forecast',
                 fontsize=13, fontweight='bold', pad=11, color='#1A1A1A')

    # Legend
    line_legend = [
        plt.Line2D([0], [0], color='#888888', linewidth=1.0, linestyle='--', alpha=0.7, label='PD7day Raw'),
        plt.Line2D([0], [0], color='#1565C0', linewidth=2.5, label='Calibrated (0\u201324h)'),
        plt.Line2D([0], [0], color='#1565C0', linewidth=1.6, alpha=0.65, label='Calibrated (24\u201372h)'),
        plt.Line2D([0], [0], color='#1565C0', linewidth=1.2, alpha=0.45, linestyle=':', label='Calibrated (72h+)'),
        mpatches.Patch(color='#BBDEFB', alpha=0.6, label='p10\u2013p90 band'),
        plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='#C62828',
                   markersize=6, label='Daily max'),
        plt.Line2D([0], [0], marker='o', color='w', markerfacecolor='#1B5E20',
                   markersize=6, label='Daily min'),
    ]
    if confirmed_indices:
        line_legend.append(plt.Line2D([0], [0], marker='^', color='w',
                                      markerfacecolor='#C62828', markersize=8,
                                      label='Confirmed Spike'))
    if candidate_indices:
        line_legend.append(plt.Line2D([0], [0], marker='^', color='w',
                                      markerfacecolor='#AAAAAA', markersize=6,
                                      label='Candidate Spike (1 run)'))
    if zone_24h is not None:
        line_legend.append(plt.Line2D([0], [0], color='#666688', linewidth=0.8,
                                      linestyle='--', alpha=0.3,
                                      label='24h confidence boundary'))
    # Add legend entries for any notice types actually present in this chart
    NOTICE_LEGEND = {
        ("LOR", 1): ("#F39C12", "LOR1 \u2014 Reserve notice"),
        ("LOR", 2): ("#E67E22", "LOR2 \u2014 Reserve notice"),
        ("LOR", 3): ("#C0392B", "LOR3 \u2014 Reserve notice"),
        ("MSL", 1): ("#8E44AD", "MSL1 \u2014 Min load notice"),
        ("MSL", 2): ("#7D3C98", "MSL2 \u2014 Min load notice"),
        ("MSL", 3): ("#6C3483", "MSL3 \u2014 Min load notice"),
    }
    for key in sorted(notice_types_present):
        if key in NOTICE_LEGEND:
            col, lbl = NOTICE_LEGEND[key]
            line_legend.append(mpatches.Patch(color=col, alpha=0.5, label=lbl))
    ax.legend(handles=line_legend, loc='upper right', fontsize=8.5,
              framealpha=0.92, edgecolor='#CCCCCC', borderpad=0.7)

    ax.set_xlim(times[0], times[-1] + datetime.timedelta(minutes=30))
    fig.tight_layout(pad=1.2)

    # Daily extreme, day divider and confidence boundary labels are placed here
    # and not at their draw sites, for the same reason the callouts are placed
    # late: where a label can go depends on where the tick labels, the legend
    # frame and the clip line strip actually landed, and that is only knowable
    # once everything is drawn and the axes are at their final size. This has to
    # run BEFORE the callouts are created, because callout placement treats these
    # labels as obstacles and must see their final positions (#93).
    label_placement = place_movable_labels(fig, ax, movable_labels,
                                           other_axes=(ax2,))
    # Honest degradation, reported by kind rather than as one count, because the
    # two mean different things to a reader of the chart: a dropped daily extreme
    # has lost its number and kept its dot, while a kept label is still readable
    # and is overlapping something.
    dropped = [lbl for lbl, mode in label_placement if mode == "dropped"]
    kept_dirty = [lbl for lbl, mode in label_placement
                  if mode in ("least_overlap", "default")]
    if dropped:
        _LOGGER.debug(
            "forecast chart: %d daily extreme label(s) had no offset clear of "
            "the other chart text and were dropped, their markers remain: %s",
            len(dropped), ", ".join(dropped),
        )
    if kept_dirty:
        _LOGGER.debug(
            "forecast chart: %d label(s) had no clear offset and were kept at "
            "the least overlapping one: %s",
            len(kept_dirty), ", ".join(kept_dirty),
        )

    # Spike callouts last of all, after the labels above have taken their final
    # positions, so everything a callout has to keep away from is drawn and
    # settled and the axes are at their final size. Each is created at the first
    # tier and then moved by _place_spike_callouts.
    if callout_peaks:
        callout_artists = [
            ax.annotate(
                f'raw ${max_raw:.2f}/kWh',
                xy=(peak_time, CLIP_Y),
                xytext=(_CALLOUT_X_OFFSETS_PT[0], _CALLOUT_Y_OFFSETS_PT[0]),
                textcoords='offset points',
                fontsize=7.5, color='#C62828', ha='left', va='bottom',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                          edgecolor='#C62828', alpha=0.9),
                arrowprops=dict(arrowstyle='->', color='#C62828', lw=1.2),
                zorder=10,
            )
            for max_raw, peak_time in callout_peaks
        ]
        placement = _place_spike_callouts(fig, ax, callout_artists,
                                          other_axes=(ax2,))
        degraded = sum(1 for _label, mode in placement if mode != "leader")
        if degraded:
            _LOGGER.debug(
                "forecast chart: %d spike callout label(s) could not take a "
                "leader line without crossing other text, so they sit beside "
                "their marker or were dropped", degraded,
            )

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=110, bbox_inches='tight', facecolor='white')
    buf.seek(0)
    return buf.getvalue()
