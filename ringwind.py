#!/usr/bin/env python
"""Name every GeoJSON ring wound against the convention you declared, exit
non-zero, and never rewind one without --apply.

Esri winds an exterior ring clockwise. RFC 7946 asks for counter-clockwise.
Most viewers ignore the difference, which is what makes it dangerous. A
spherical-geometry engine does not ignore it: BigQuery GEOGRAPHY, S2, PostGIS
geography and several tile builders read a clockwise exterior as its own
complement. A point-in-polygon test against that complement returns true
everywhere outside the boundary, so a geofence admits exactly what it was built
to exclude, consistently, which reads as working.

Why not geojson-rewind or shapely.orient. Both rewind correctly and both are
one call. geojson-rewind walks a whole document and returns it wound the RFC
way; shapely.orient sets the sign on a polygon you already hold. They are the
right tools for fixing a file. Neither one tells you a file was wrong: they
return corrected output and report nothing, so a pipeline that pipes every file
through one can never fail on winding and never learns which upstream export
produces it. The gap is the refusal. This one reads, names the ring by feature,
part and index, exits 1, and rewinds only when you ask with --apply.

    python ringwind.py boundary.geojson
    python ringwind.py boundary.geojson --esri
    cat boundary.geojson | python ringwind.py -
    python ringwind.py boundary.geojson --out fixed.geojson --apply
    python ringwind.py --self-test

Exit codes: 0 every ring matches the convention, 1 at least one ring does not,
2 the input could not be read or holds no polygon, 64 usage error.
"""

from __future__ import print_function

import argparse
import collections
import io
import json
import math
import os
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Winding the exterior ring takes when no flag is given. False is
# counter-clockwise, which is what RFC 7946 section 3.1.6 asks for and what a
# spherical engine reads as "the inside is the small side". --esri selects
# Esri's direction for one run. A shop whose files are all meant to stay in
# Esri's direction sets this to True instead, and then --esri is a no-op.
DEFAULT_EXTERIOR_CW = False

# Positions a closed linear ring needs before it encloses any area. RFC 7946
# section 3.1.6 sets the same floor. Three positions is two distinct vertices
# and a line segment, which has no winding to measure.
MIN_RING_POSITIONS = 4

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Finding codes.
WINDING = "WINDING"          # the ring is wound against the convention
DEGENERATE = "DEGENERATE"    # the ring has no measurable winding at all
SHORT_RING = "SHORT_RING"    # the ring is too short to enclose anything

ROLE_EXTERIOR = "exterior"
ROLE_HOLE = "hole"


class Finding(object):
    """One ring that the convention rejects, addressed the way a fix needs it.

    feature, part and ring are indices into the document as read, so a finding
    points at coordinates[part][ring] of feature number `feature`. That triple
    is the whole address: a message naming only the file is useless on a
    MultiPolygon with 32 parts.
    """

    def __init__(self, code, feature, part, ring, role, message):
        self.code = code
        self.feature = feature
        self.part = part
        self.ring = ring
        self.role = role
        self.message = message

    def where(self):
        return "feature %d part %d ring %d (%s)" % (
            self.feature, self.part, self.ring, self.role)

    def __repr__(self):
        return "Finding(%s, %d, %d, %d)" % (
            self.code, self.feature, self.part, self.ring)


# What audit() counted while it walked, so the report can say how much it
# actually looked at. A clean verdict over nothing is the failure mode this
# guards against.
Report = collections.namedtuple(
    "Report", "findings features parts rings skipped")


# ----------------------------------------------------------------- pure core

def shoelace(ring):
    """Twice the signed area of a closed ring. Positive means clockwise.

    This is the author's own lon/lat shoelace, kept in the same form it was
    measured in. The sign convention is the one that falls out of summing
    (x2 - x1) * (y2 + y1) over the edges: positive for a clockwise ring in a
    normal y-up coordinate system.

    The sum is returned rather than a boolean because zero is a third answer.
    Three collinear points give exactly 0.0, and the author's original
    `ring_is_cw` tested `sum > 0`, so a collinear ring answered False and read
    as a correctly wound counter-clockwise exterior. That ring is not
    counter-clockwise. It has no direction at all.

    The caller must pass a closed ring. zip(ring, ring[1:]) walks the edges
    between consecutive positions and never adds the closing edge back to the
    first position, so an unclosed ring is short one edge and the sum is wrong.
    """
    return sum((b[0] - a[0]) * (b[1] + a[1]) for a, b in zip(ring, ring[1:]))


def ring_is_cw(ring):
    """True when a closed ring is wound clockwise. Verbatim from the source.

    Kept as its own function because it is the predicate people port out of
    this file, and because the self-test pins the exact case where it is not
    enough on its own. Use classify_ring for anything that must be correct.
    """
    return shoelace(ring) > 0


def closed(ring):
    """The ring with its first position repeated at the end, if it is not.

    A ring that already closes is returned unchanged, not closed twice. A
    duplicated closing position adds a zero-length edge, which is harmless for
    the shoelace sum and wrong in the written file.
    """
    if not ring:
        return ring
    if ring[0] == ring[-1]:
        return ring
    return list(ring) + [ring[0]]


def polygons_from_geojson(obj):
    """Every Polygon and MultiPolygon in a GeoJSON object, as ring lists.

    Accepts a FeatureCollection, a Feature, a GeometryCollection or a bare
    geometry, because county boundaries arrive as all four and re-exporting
    one is not the operator's job.

    The lists that come back are the live lists json.load built, not copies, so
    rewinding a ring in place changes the document that gets written.
    """
    # A file off disk holds whatever JSON allows. A string or a number
    # where a geometry belongs has to read as "no polygon here", not as an
    # AttributeError raised three frames below the caller.
    kind = obj.get("type") if isinstance(obj, dict) else None
    if kind == "FeatureCollection":
        out = []
        for feat in obj.get("features") or []:
            out.extend(polygons_from_geojson(feat))
        return out
    if kind == "Feature":
        # An exported county boundary routinely carries one feature with a
        # null geometry. Refusing the whole file over it helps nobody.
        geom = obj.get("geometry")
        return polygons_from_geojson(geom) if geom else []
    if kind == "GeometryCollection":
        out = []
        for geom in obj.get("geometries") or []:
            out.extend(polygons_from_geojson(geom))
        return out
    if kind == "Polygon":
        return [obj["coordinates"]]
    if kind == "MultiPolygon":
        return list(obj["coordinates"])
    raise ValueError("no polygon found in GeoJSON of type %r" % (kind,))


def features_of(obj):
    """The features of a document, in order, so a finding can name one.

    polygons_from_geojson flattens a FeatureCollection, which loses the feature
    number. Splitting the walk in two keeps that walker unchanged and still
    gives every finding a feature index.
    """
    if (obj or {}).get("type") == "FeatureCollection":
        return list(obj.get("features") or [])
    return [obj]


def parts_of(feature):
    """Polygon parts of one feature, or None when it carries no polygon.

    A line layer, a point layer and a mixed FeatureCollection are all normal
    inputs, and a winding checker has nothing to say about any of them. None
    distinguishes "skipped on purpose" from "a polygon with no rings", which
    the report counts separately.
    """
    try:
        return polygons_from_geojson(feature)
    except ValueError:
        return None


def classify_ring(ring, feature, part, index, exterior_cw):
    """The finding for one ring, or None when the ring matches the convention.

    Ring 0 of a part is the exterior and every later ring is a hole, which is
    the GeoJSON rule and also the rule the source script assumed. A hole takes
    the opposite winding from its exterior, so one flag decides both.
    """
    role = ROLE_EXTERIOR if index == 0 else ROLE_HOLE
    want_cw = exterior_cw if index == 0 else not exterior_cw
    ring = closed(ring)

    if len(ring) < MIN_RING_POSITIONS:
        return Finding(
            SHORT_RING, feature, part, index, role,
            "a ring of %d position(s) encloses no area. It is not scored, "
            "because there is no winding to score." % len(ring))

    area2 = shoelace(ring)
    if area2 == 0.0:
        # The refusal this tool exists for. A collinear or self-cancelling ring
        # sums to exactly zero, the original predicate read that as
        # counter-clockwise, and the ring shipped as a correct exterior.
        return Finding(
            DEGENERATE, feature, part, index, role,
            "the shoelace sum is exactly 0, so the ring encloses no area and "
            "has no winding. A > 0 test reads this as counter-clockwise.")

    if not math.isfinite(area2):
        # The same trap from the other side. json.loads accepts bare NaN and
        # Infinity, so a coordinate can arrive non-finite from a file that
        # parses cleanly. Every comparison against a NaN sum is False, so
        # `> 0` answers False and the ring reads as a correct
        # counter-clockwise exterior exactly as a collinear one did.
        return Finding(
            DEGENERATE, feature, part, index, role,
            "the shoelace sum is %r, so the ring has no winding. A coordinate "
            "is NaN or infinite, and a > 0 test reads that as "
            "counter-clockwise." % (area2,))

    if (area2 > 0) != want_cw:
        return Finding(
            WINDING, feature, part, index, role,
            "wound %s; this %s must be %s" % (
                _direction(area2 > 0), role, _direction(want_cw)))
    return None


def _direction(is_cw):
    return "clockwise" if is_cw else "counter-clockwise"


def convention_name(exterior_cw):
    """One line naming the convention in force, for the report header."""
    if exterior_cw:
        return ("Esri (exterior clockwise, holes counter-clockwise)")
    return ("RFC 7946 (exterior counter-clockwise, holes clockwise)")


def audit(obj, exterior_cw):
    """Walk a GeoJSON document and report every ring the convention rejects.

    Pure: it reads the parsed document and writes nothing. Returns a Report so
    the caller can tell a clean file from a file that held nothing to check.
    """
    findings = []
    features = features_of(obj)
    parts = rings = skipped = 0
    for f_i, feature in enumerate(features):
        got = parts_of(feature)
        if got is None:
            skipped += 1
            continue
        for p_i, part in enumerate(got):
            parts += 1
            for r_i, ring in enumerate(part):
                rings += 1
                finding = classify_ring(ring, f_i, p_i, r_i, exterior_cw)
                if finding is not None:
                    findings.append(finding)
    return Report(findings, len(features), parts, rings, skipped)


def rewind(obj, exterior_cw):
    """Reverse every wrongly wound ring in place. Returns how many it turned.

    Only a WINDING finding is acted on. A DEGENERATE ring is left exactly as it
    was: reversing a ring with no measurable direction rewrites the file
    without fixing anything, and would hide the one defect worth reporting. A
    SHORT_RING is left alone for the same reason.

    An unclosed ring is closed as it is rewound, because a ring whose winding
    was scored on a closing edge that is not in the file would otherwise be
    written back still missing it.
    """
    turned = 0
    for f_i, feature in enumerate(features_of(obj)):
        got = parts_of(feature)
        if got is None:
            continue
        for p_i, part in enumerate(got):
            for r_i, ring in enumerate(part):
                finding = classify_ring(ring, f_i, p_i, r_i, exterior_cw)
                if finding is None or finding.code != WINDING:
                    continue
                # Slice assignment, so the change lands in the document json
                # built rather than in a copy that is thrown away.
                ring[:] = closed(ring)[::-1]
                turned += 1
    return turned


def describe(report, exterior_cw):
    """The report, as lines. Separated from printing so the self-test reads it."""
    lines = ["convention: %s" % convention_name(exterior_cw)]
    lines.append("scanned: %d feature(s), %d part(s), %d ring(s)%s" % (
        report.features, report.parts, report.rings,
        ", %d feature(s) skipped as non-polygon" % report.skipped
        if report.skipped else ""))
    if not report.findings:
        return lines
    lines.append("")
    for f in report.findings:
        lines.append("%s: %s" % (f.where(), f.code))
        lines.append("    %s" % f.message)
    return lines


# ------------------------------------------------------------------ io layer

def load(path):
    """The parsed document, or a string saying why it could not be read.

    Returns (obj, None) or (None, "message"). A tool that exits 2 on an
    unreadable file has to distinguish that from a file it read and disliked,
    and an exception three frames up cannot make that distinction.
    """
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8-sig")
    except IOError as exc:
        return None, "cannot read %s: %s" % (path, exc)
    except UnicodeDecodeError as exc:
        return None, "%s is not UTF-8 text: %s" % (path, exc)
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, "%s is not valid JSON: %s" % (path, exc)


def write_json(obj, path):
    """Write the document to path, replacing it only once it is fully written.

    A half-written boundary file is worse than an unrewound one, so the new
    text goes to a temporary file beside the target and is moved over it.
    os.replace, not os.rename: os.rename refuses an existing target on Windows
    and silently overwrites on POSIX, so the in-place rewrite would work on the
    server and fail on the workstation. The temporary file shares the target's
    directory because the move is only atomic within one filesystem.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".ringwind.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        os.remove(tmp)
        raise


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="ringwind.py",
        description="Name every GeoJSON ring wound against the convention you "
                    "declared, and exit non-zero.",
        epilog="Nothing is written without --apply. A degenerate ring is never "
               "rewound, because it has no winding to correct.",
        allow_abbrev=False,
    )
    ap.add_argument("path", nargs="?", metavar="GEOJSON",
                    help="the .geojson file to check, or - for standard input")
    ap.add_argument("--esri", action="store_true",
                    help="expect Esri's direction instead: exterior clockwise, "
                         "holes counter-clockwise. Off by default, so the "
                         "default is RFC 7946.")
    ap.add_argument("--out", metavar="PATH",
                    help="where a rewound document is written. Without it "
                         "--apply rewrites the input file.")
    ap.add_argument("--apply", action="store_true",
                    help="rewind the wrongly wound rings and write the result. "
                         "Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.path:
        print("error: pass a .geojson file, or - for standard input. Use "
              "--self-test to verify the tool without a file.", file=sys.stderr)
        return 64

    if args.apply and args.path == "-" and not args.out:
        # Rewriting "in place" has no meaning for a pipe, and writing the
        # rewound document to stdout would mix it with the report.
        print("error: --apply on standard input needs --out. There is no file "
              "to rewrite.", file=sys.stderr)
        return 64

    if args.out and not args.apply:
        print("note: --out was given without --apply. Nothing will be written.")

    obj, problem = load(args.path)
    if problem is not None:
        print("error: %s" % problem, file=sys.stderr)
        return 2

    # The configured default, overridden for this run by --esri. Reading the
    # constant here is what makes it a knob: a flag alone would leave it
    # declared and dead.
    exterior_cw = args.esri or DEFAULT_EXTERIOR_CW

    report = audit(obj, exterior_cw)
    if report.rings == 0:
        # A clean verdict over a file holding no ring is the worst answer this
        # could give: it is indistinguishable from a file that passed.
        print("error: %s holds no polygon ring to check." % args.path,
              file=sys.stderr)
        return 2

    print("ringwind: %s" % args.path)
    for line in describe(report, exterior_cw):
        print(line)

    wrong = [f for f in report.findings if f.code == WINDING]
    stuck = [f for f in report.findings if f.code != WINDING]

    if not report.findings:
        print("")
        print("VERDICT: CLEAN")
        return 0

    print("")
    if not args.apply:
        if wrong:
            print("Check only. Nothing was written. Re-run with --apply to "
                  "rewind %d ring(s)." % len(wrong))
        print("FAIL: %d ring(s) do not match the convention." %
              len(report.findings))
        return 1

    if stuck:
        # Refusing the whole write is the point. Half a boundary rewound and
        # half of it degenerate is a file nobody can reason about afterwards.
        print("Refusing to write: %d ring(s) have no winding to correct. "
              "Fix the geometry first." % len(stuck))
        print("FAIL: %d ring(s) do not match the convention." %
              len(report.findings))
        return 1

    turned = rewind(obj, exterior_cw)
    target = args.out or args.path
    write_json(obj, target)
    print("rewound %d ring(s) and wrote %s" % (turned, target))
    print("FAIL: %d ring(s) did not match the convention." %
          len(report.findings))
    return 1


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core, then over the io layer.

    Nothing here opens a network socket, a database or arcpy. The io half
    writes GeoJSON into a temporary directory and drives main() over it,
    because reading stdin, rewriting a file in place and the exit codes cannot
    be tested any other way.
    """
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def codes(report):
        return [f.code for f in report.findings]

    def run(argv):
        """main()'s exit code and everything it printed, as one pair.

        A line the README promises a reader will see has to be asserted, not
        described. Without this, deleting the print leaves the suite green.
        """
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            code = main(argv)
            return code, sys.stdout.getvalue()
        finally:
            sys.stdout = saved

    def addrs(report):
        return [(f.feature, f.part, f.ring) for f in report.findings]

    # Unit squares, closed. CCW is the RFC direction; CW is Esri's.
    ccw = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    cw = [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]
    # A hole inside the CCW square, wound the RFC way for a hole.
    hole_cw = [[0.2, 0.2], [0.2, 0.8], [0.8, 0.8], [0.8, 0.2], [0.2, 0.2]]
    hole_ccw = hole_cw[::-1]
    # A second square, far enough away that it is a separate part.
    ccw_b = [[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]

    def poly(*rings):
        return {"type": "Polygon", "coordinates": list(rings)}

    def multi(*parts):
        return {"type": "MultiPolygon", "coordinates": list(parts)}

    def fc(*geoms):
        return {"type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {},
                              "geometry": g} for g in geoms]}

    print("ringwind self-test: no file, no network, no credentials")
    print("-" * 68)

    # ---- the shoelace sign
    check(ring_is_cw(ccw) is False,
          "a counter-clockwise unit square is not clockwise")
    check(ring_is_cw(cw) is True,
          "a clockwise unit square is clockwise")
    check(shoelace(ccw) == -2.0,
          "the shoelace sum of the unit square is twice its area, signed")
    check(shoelace(cw) == 2.0,
          "reversing the square flips the sign and keeps the magnitude")
    check(shoelace(ccw) == -shoelace(ccw[::-1]) != 0,
          "reversing any ring negates its shoelace sum, and the sum is not "
          "zero, so the symmetry is not two zeroes agreeing")
    check(ring_is_cw(hole_cw) is True,
          "the hole cut into that square is wound clockwise")

    # ---- the degenerate ring. This is why shoelace returns a number.
    collinear = [[0, 0], [1, 1], [2, 2], [0, 0]]
    check(shoelace(collinear) == 0,
          "three collinear points give a shoelace sum of exactly 0")
    check(ring_is_cw(collinear) is False,
          "a > 0 test calls that collinear ring counter-clockwise, which "
          "reads as a correct exterior  <-- pinned defect")
    f = classify_ring(collinear, 0, 0, 0, DEFAULT_EXTERIOR_CW)
    check(f is not None and f.code == DEGENERATE,
          "classify_ring reports DEGENERATE instead of passing it  "
          "<-- pinned defect")
    check(f is not None and "no winding" in f.message,
          "the degenerate message says the ring has no winding, not that it "
          "is wound the wrong way")
    # A bow tie encloses two equal lobes of opposite sign, so it cancels too.
    bowtie = [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]
    check(shoelace(bowtie) == 0,
          "a self-cancelling bow tie also sums to exactly 0")
    check(classify_ring(bowtie, 0, 0, 0, False).code == DEGENERATE,
          "the bow tie is DEGENERATE as well, not a wrongly wound exterior")

    # ---- non-finite coordinates. json.loads parses bare NaN and Infinity, so
    # a ring can arrive with no measurable winding from a file that is valid
    # JSON, and every comparison against a NaN sum answers False.
    nan_ring = json.loads(
        '[[0,0],[NaN,0],[1,1],[0,1],[0,0]]')
    check(nan_ring[1][0] != nan_ring[1][0],
          "a bare NaN in a GeoJSON coordinate parses, it does not raise")
    check(shoelace(nan_ring) != shoelace(nan_ring),
          "the shoelace sum of a ring holding NaN is itself NaN")
    check(ring_is_cw(nan_ring) is False,
          "a > 0 test calls that NaN ring counter-clockwise, which reads as a "
          "correct exterior  <-- pinned defect")
    f = classify_ring(nan_ring, 0, 0, 0, DEFAULT_EXTERIOR_CW)
    check(f is not None and f.code == DEGENERATE,
          "classify_ring reports the NaN ring DEGENERATE instead of clean  "
          "<-- pinned defect")
    check(f is not None and "NaN or infinite" in f.message,
          "the message names the non-finite coordinate rather than a winding")
    inf_ring = json.loads('[[0,0],[Infinity,0],[1,1],[0,1],[0,0]]')
    check(classify_ring(inf_ring, 0, 0, 0, False).code == DEGENERATE,
          "an infinite coordinate is DEGENERATE too, not a wound exterior")
    check(classify_ring(nan_ring, 0, 0, 0, True).code == DEGENERATE,
          "--esri does not turn a NaN ring into a valid one either")
    doc = {"type": "Polygon", "coordinates": [list(nan_ring)]}
    check(rewind(doc, False) == 0,
          "a NaN ring is never rewound: reversing it measures nothing  "
          "<-- pinned defect")

    # ---- rings too short to score
    two = [[0, 0], [1, 1]]
    f = classify_ring(two, 0, 0, 0, False)
    check(f is not None and f.code == SHORT_RING,
          "a two-vertex ring is rejected, not scored")
    check("not scored" in f.message,
          "the short-ring message says the ring was not scored")
    check(classify_ring([[0, 0], [1, 1], [0, 0]], 0, 0, 0, False).code
          == SHORT_RING,
          "a closed ring of three positions is two vertices and is rejected")
    check(classify_ring([], 0, 0, 0, False).code == SHORT_RING,
          "an empty ring is rejected rather than raising")
    check(classify_ring(ccw, 0, 0, 0, False) is None,
          "four distinct vertices are enough to score")

    # ---- closing a ring
    check(closed(ccw) is ccw,
          "an already-closed ring is returned unchanged, not closed twice")
    check(len(closed(ccw)) == 5,
          "closing an already-closed ring does not add a fifth position")
    open_ring = [[0, 0], [1, 0], [1, 1], [0, 1]]
    check(closed(open_ring) == ccw,
          "an unclosed ring gains its first position at the end")
    check(open_ring == [[0, 0], [1, 0], [1, 1], [0, 1]],
          "closing a ring does not mutate the caller's list")
    check(closed([]) == [],
          "an empty ring closes to an empty ring rather than raising")
    # The closing edge of this square is vertical and contributes nothing, so
    # the case that proves closing matters needs a ring whose last edge is not.
    open_tri = [[0, 0], [2, 0], [1, 2]]
    check(shoelace(open_tri) == -2.0 and shoelace(closed(open_tri)) == -4.0,
          "the closing edge is half the sum on this triangle, so scoring an "
          "unclosed ring would halve it  <-- pinned defect")
    check(classify_ring(open_tri, 0, 0, 0, False) is None
          and classify_ring(open_tri, 0, 0, 1, False).code == WINDING,
          "that unclosed triangle scores as counter-clockwise either way, so "
          "the halving is silent until a ring changes sign")
    check(classify_ring(open_ring, 0, 0, 0, False) is None,
          "classify_ring closes the ring before scoring it, so an unclosed "
          "counter-clockwise square still passes")

    # ---- the walker
    check(len(polygons_from_geojson(poly(ccw))) == 1,
          "a bare Polygon is one part")
    check(len(polygons_from_geojson(multi([ccw], [ccw_b]))) == 2,
          "a MultiPolygon of two parts is two parts")
    check(len(polygons_from_geojson(fc(poly(ccw), multi([ccw], [ccw_b])))) == 3,
          "a FeatureCollection flattens to every part it holds")
    check(polygons_from_geojson(
        {"type": "Feature", "properties": {}, "geometry": None}) == [],
          "a Feature with a null geometry contributes no part")
    check(len(polygons_from_geojson(
        {"type": "GeometryCollection",
         "geometries": [poly(ccw), poly(ccw_b)]})) == 2,
          "a GeometryCollection contributes each of its geometries")
    raises(lambda: polygons_from_geojson({"type": "Point",
                                          "coordinates": [0, 0]}),
           "a Point raises rather than returning no rings")
    raises(lambda: polygons_from_geojson(None),
           "a null document raises rather than reading as empty")
    raises(lambda: polygons_from_geojson("Polygon"),
           "a bare string where a geometry belongs raises ValueError, not "
           "AttributeError  <-- pinned defect")
    check(parts_of(["Polygon"]) is None,
          "a JSON array where a feature belongs is skipped, not a traceback")
    check(parts_of({"type": "Point", "coordinates": [0, 0]}) is None,
          "parts_of returns None for a non-polygon instead of raising")
    check(parts_of(poly(ccw)) == [[ccw]],
          "parts_of returns the rings of a real polygon, not an empty list")
    check(len(features_of(fc(poly(ccw), poly(ccw_b)))) == 2,
          "features_of splits a two-feature FeatureCollection into two, not "
          "into the one document it was handed")
    check(len(features_of(poly(ccw))) == 1,
          "a bare geometry is one feature for addressing purposes")
    check(features_of({"type": "FeatureCollection"}) == [],
          "a FeatureCollection with no features key is no features")

    # ---- the audit, addressed by feature, part and ring
    r = audit(poly(ccw), False)
    check(codes(r) == [] and r.rings == 1,
          "a correctly wound exterior alone is clean")
    r = audit(poly(ccw, hole_cw), False)
    check(codes(r) == [] and r.rings == 2,
          "a correct exterior with a correctly wound hole is clean")
    r = audit(poly(ccw, hole_ccw), False)
    check(codes(r) == [WINDING] and addrs(r) == [(0, 0, 1)],
          "a correct exterior with a wrongly wound hole gives one violation "
          "at feature 0, part 0, ring 1")
    check(r.findings[0].role == ROLE_HOLE,
          "that violation names the ring as a hole, not an exterior")
    check(repr(r.findings[0]) == "Finding(WINDING, 0, 0, 1)",
          "a finding reprs as its code and its three indices")
    check("counter-clockwise" in r.findings[0].message
          and "clockwise" in r.findings[0].message,
          "the message says which direction it found and which it wanted")
    r = audit(poly(cw, hole_ccw), False)
    check(addrs(r) == [(0, 0, 0), (0, 0, 1)],
          "an Esri-wound polygon reports both of its rings under RFC 7946")

    # ---- per-part indices
    r = audit(multi([ccw], [cw]), False)
    check(addrs(r) == [(0, 1, 0)],
          "a MultiPolygon reports the second part by its own part index")
    r = audit(multi([ccw, hole_ccw], [cw, hole_ccw]), False)
    check(addrs(r) == [(0, 0, 1), (0, 1, 0), (0, 1, 1)],
          "every ring of a MultiPolygon is addressed by part and ring")
    r = audit(fc(poly(cw), poly(ccw), poly(cw)), False)
    check(addrs(r) == [(0, 0, 0), (2, 0, 0)],
          "a FeatureCollection addresses each violation by its feature index")
    check(r.features == 3 and r.parts == 3 and r.rings == 3,
          "the report counts what it walked, not only what it rejected")

    # ---- the flattened-parts defect the source script shipped
    # Its ring walker appended every part of every geometry to one flat
    # list, so part 2's exterior arrived as ring 1 of part 1 and was
    # scored as a hole.
    flattened = poly(ccw, ccw_b)
    r = audit(flattened, False)
    check([f is not None for f in
           [classify_ring(x, 0, 0, i, False)
            for i, x in enumerate(flattened["coordinates"])]] == [False, True],
          "two separate counter-clockwise parts flattened into one ring list "
          "measure [False, True]: one exterior and one hole  <-- pinned defect")
    check(codes(r) == [WINDING] and addrs(r) == [(0, 0, 1)],
          "the flattened list therefore reports a violation on a ring that is "
          "wound correctly for the part it really belongs to")
    r = audit(multi([ccw], [ccw_b]), False)
    check(codes(r) == [] and r.parts == 2,
          "the same two squares as a MultiPolygon are two exteriors and clean")

    # ---- the Esri flag inverts every verdict
    clean_rfc = poly(ccw, hole_cw)
    check(codes(audit(clean_rfc, False)) == [],
          "the RFC-wound polygon is clean under the default convention")
    r = audit(clean_rfc, True)
    check(addrs(r) == [(0, 0, 0), (0, 0, 1)],
          "--esri reports every ring of that same polygon  <-- pinned defect")
    clean_esri = poly(cw, hole_ccw)
    check(codes(audit(clean_esri, True)) == [],
          "the Esri-wound polygon is clean under --esri")
    check(len(audit(clean_esri, False).findings) == 2,
          "and reports both rings under the default convention")
    check(classify_ring(collinear, 0, 0, 0, True).code == DEGENERATE,
          "--esri does not turn a degenerate ring into a valid one")
    check(classify_ring(two, 0, 0, 0, True).code == SHORT_RING,
          "--esri does not turn a two-vertex ring into a valid one")

    # ---- non-polygon features are skipped, not scored
    mixed = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Point", "coordinates": [0, 0]}},
        {"type": "Feature", "properties": {}, "geometry": poly(cw)}]}
    r = audit(mixed, False)
    check(r.skipped == 1 and addrs(r) == [(1, 0, 0)],
          "a point beside a polygon is skipped and the polygon keeps its own "
          "feature index")
    check(audit({"type": "Point", "coordinates": [0, 0]}, False).rings == 0,
          "a document of nothing but points scores no ring at all")

    # ---- rewinding, in memory
    doc = poly(list(ccw), list(hole_ccw))
    check(rewind(doc, False) == 1,
          "rewinding the wrongly wound hole turns exactly one ring")
    check(codes(audit(doc, False)) == [],
          "the rewound document is then clean")
    check(doc["coordinates"][0] == ccw,
          "the exterior that was already correct is untouched")
    check(doc["coordinates"][1] == hole_ccw[::-1],
          "the hole is the same ring reversed")
    check(len(doc["coordinates"][1]) == len(hole_ccw),
          "rewinding does not add or drop a position")
    doc = poly(list(ccw))
    check(rewind(doc, False) == 0,
          "a clean document is not rewritten at all")
    doc = poly([list(p) for p in collinear])
    check(rewind(doc, False) == 0,
          "a degenerate ring is never rewound: reversing it fixes nothing  "
          "<-- pinned defect")
    doc = poly(list(two))
    check(rewind(doc, False) == 0,
          "a two-vertex ring is never rewound either")
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Point", "coordinates": [0, 0]}},
        {"type": "Feature", "properties": {}, "geometry": poly(list(cw))}]}
    check(rewind(doc, False) == 1,
          "rewind skips the point feature and still turns the polygon")
    doc = poly(list(open_ring[::-1]))
    check(rewind(doc, False) == 1 and doc["coordinates"][0][0]
          == doc["coordinates"][0][-1],
          "an unclosed ring is closed as it is rewound  <-- pinned defect")
    check(codes(audit(doc, False)) == [],
          "and the closed, rewound ring passes the audit")
    doc = multi([list(cw)], [list(ccw_b)])
    check(rewind(doc, True) == 1,
          "under --esri it is the counter-clockwise part that gets turned")

    # ---- the report text
    lines = describe(audit(poly(ccw, hole_cw), False), False)
    check("RFC 7946" in lines[0] and "counter-clockwise" in lines[0],
          "the header names the convention in force")
    check("Esri" in describe(audit(poly(cw), True), True)[0],
          "--esri says so in the header instead")
    check("2 ring(s)" in lines[1],
          "the scanned line reports the ring count")
    check("skipped" not in lines[1],
          "a document with nothing skipped does not mention skipping")
    check("1 feature(s) skipped" in describe(audit(mixed, False), False)[1],
          "a document with a skipped feature says how many")
    lines = describe(audit(poly(ccw, hole_ccw), False), False)
    check(any("feature 0 part 0 ring 1 (hole): WINDING" in ln
              for ln in lines),
          "a finding prints its address, its role and its code")

    # ---- argument parsing. The destructive flag is off unless asked for.
    args = _parse(["a.geojson"])
    check(args.apply is False,
          "--apply is off by default, so nothing is ever written by accident")
    check(args.esri is False,
          "--esri is off by default, so the default is RFC 7946")
    check(args.out is None and args.self_test is False,
          "--out and --self-test are off by default")
    check(args.path == "a.geojson",
          "the geojson path is read from the first positional argument")
    check(_parse(["a.geojson", "--apply"]).apply is True,
          "--apply is read when it is given")
    check(_parse(["a.geojson", "--esri"]).esri is True,
          "--esri is read when it is given")
    check(_parse(["a.geojson", "--out", "b.geojson"]).out == "b.geojson",
          "--out is read when it is given")
    check(_parse(["--self-test"]).self_test is True,
          "--self-test is read when it is given")
    check(_parse([]).path is None,
          "no argument at all parses, so main can print its own message")
    check(_parse(["-"]).path == "-",
          "a single dash is accepted as the path and means standard input")
    saved_err = sys.stderr
    sys.stderr = io.StringIO()
    try:
        _parse(["a.geojson", "--ap"])
        refused = False
    except SystemExit as exc:
        refused = exc.code == 2
    finally:
        sys.stderr = saved_err
    check(refused, "a unique prefix of --apply is refused, not read as --apply  "
                   "<-- pinned defect")

    # ---- the io layer, against real files
    tmp = tempfile.mkdtemp(prefix="ringwind-selftest-")
    try:
        def path(name):
            return os.path.join(tmp, name)

        def put(name, obj):
            with open(path(name), "w") as fh:
                json.dump(obj, fh)
            return path(name)

        def read(name):
            with open(path(name)) as fh:
                return json.load(fh)

        clean = put("clean.geojson", poly(ccw, hole_cw))
        dirty = put("dirty.geojson", poly(ccw, hole_ccw))
        bad = put("degenerate.geojson", poly(collinear))
        points = put("points.geojson",
                     {"type": "Point", "coordinates": [0, 0]})

        obj, problem = load(clean)
        check(problem is None and obj["type"] == "Polygon",
              "a real file loads and parses")
        check(load(path("nope.geojson"))[1].startswith("cannot read"),
              "a missing file reports that it cannot be read")

        with open(path("broken.json"), "w") as fh:
            fh.write("{not json")
        check("not valid JSON" in load(path("broken.json"))[1],
              "a malformed file reports invalid JSON rather than raising")

        with open(path("bom.geojson"), "wb") as fh:
            fh.write(b"\xef\xbb\xbf")
            fh.write(json.dumps(poly(ccw)).encode("utf-8"))
        check(load(path("bom.geojson"))[1] is None,
              "a UTF-8 BOM does not stop the file parsing  <-- pinned defect")

        with open(path("latin.geojson"), "wb") as fh:
            fh.write(b"{\"type\": \"Polygon\", \"n\": \"\xff\xfe\"}")
        check("not UTF-8" in load(path("latin.geojson"))[1],
              "a file that is not UTF-8 reports that, not a traceback")

        check(main([clean]) == 0,
              "a clean file exits 0")

        # DEFAULT_EXTERIOR_CW has to reach main, or it is a comment wearing a
        # constant's name. Asserting `DEFAULT_EXTERIOR_CW is False` would pass
        # whether or not anything ever read it, so drive main instead.
        esri_file = put("esri.geojson", poly(cw, hole_ccw))
        check(run([esri_file])[0] == 1,
              "an Esri-wound file fails under the shipped default")
        saved_default = globals()["DEFAULT_EXTERIOR_CW"]
        globals()["DEFAULT_EXTERIOR_CW"] = True
        try:
            code, text = run([esri_file])
            check(code == 0 and "Esri" in text,
                  "setting DEFAULT_EXTERIOR_CW makes Esri the convention for a "
                  "run with no flag at all  <-- pinned defect")
            check(run([clean])[0] == 1,
                  "and the RFC-wound file then fails, so the constant really "
                  "decides the verdict")
        finally:
            globals()["DEFAULT_EXTERIOR_CW"] = saved_default
        check(run([clean])[0] == 0 and run([esri_file])[0] == 1,
              "restoring the constant restores the RFC default")
        check(main([dirty]) == 1,
              "a file with a wrongly wound hole exits 1")
        check(main([clean, "--esri"]) == 1,
              "that same clean file exits 1 under --esri")
        check(main([bad]) == 1,
              "a degenerate ring exits 1")
        check(main([points]) == 2,
              "a file holding no polygon ring exits 2, rather than passing")
        check(main([path("nope.geojson")]) == 2,
              "a missing file exits 2")
        check(main([]) == 64,
              "no argument at all is a usage error")

        nan_file = path("nan.geojson")
        with open(nan_file, "w") as fh:
            fh.write('{"type":"Polygon","coordinates":'
                     '[[[0,0],[NaN,0],[1,1],[0,1],[0,0]]]}')
        code, text = run([nan_file])
        check(code == 1 and "DEGENERATE" in text,
              "a real file carrying a NaN coordinate exits 1 and says "
              "DEGENERATE  <-- pinned defect")
        check(run([nan_file, "--apply"])[0] == 1
              and "Refusing to write" in run([nan_file, "--apply"])[1],
              "and --apply refuses to write it rather than reversing nothing")

        # --out without --apply must not write, which is the flag's whole job.
        out = path("out.geojson")
        code, text = run([dirty, "--out", out])
        check(code == 1, "--out without --apply still reports the finding")
        check("Nothing will be written" in text,
              "--out without --apply says out loud that it wrote nothing, "
              "which is the line the README promises  <-- pinned defect")
        check("Nothing will be written" not in run([dirty])[1],
              "and a run without --out does not print that note")
        check(not os.path.exists(out),
              "--out without --apply writes nothing at all  <-- pinned defect")
        check(read("dirty.geojson") == poly(ccw, hole_ccw),
              "and leaves the input file exactly as it was")

        check(main([dirty, "--out", out, "--apply"]) == 1,
              "--apply still exits 1: the file WAS wrong")
        check(os.path.exists(out),
              "--apply with --out writes the rewound document")
        with open(out) as fh:
            written = json.load(fh)
        check(written["coordinates"][1] == hole_ccw[::-1],
              "the written hole is the reversed ring")
        check(audit(written, False).findings == [],
              "the written document passes its own audit")
        check(read("dirty.geojson") == poly(ccw, hole_ccw),
              "--out leaves the input file untouched")

        check(main([out]) == 0,
              "re-running over the rewound file exits 0")

        # --apply with no --out rewrites the input in place.
        inplace = put("inplace.geojson", poly(cw, hole_ccw))
        check(main([inplace, "--apply"]) == 1,
              "--apply with no --out reports the findings it fixed")
        check(audit(read("inplace.geojson"), False).findings == [],
              "and rewrote the input file itself")
        check(len(os.listdir(tmp)) == len([n for n in os.listdir(tmp)
                                           if not n.endswith(".tmp")]),
              "the atomic write leaves no .tmp file behind  <-- pinned defect")

        # A degenerate ring blocks the write instead of half-fixing the file.
        mix = put("mixed.geojson", poly(collinear, hole_ccw))
        before = read("mixed.geojson")
        check(main([mix, "--apply"]) == 1,
              "a document holding a degenerate ring exits 1 under --apply")
        check(read("mixed.geojson") == before,
              "and is not written at all, rather than half rewound  "
              "<-- pinned defect")

        # stdin
        saved = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(poly(ccw, hole_cw)))
            check(main(["-"]) == 0,
                  "a clean document read from standard input exits 0")
            sys.stdin = io.StringIO(json.dumps(poly(ccw, hole_ccw)))
            check(main(["-"]) == 1,
                  "a wrongly wound document read from standard input exits 1")
            sys.stdin = io.StringIO("{not json")
            check(main(["-"]) == 2,
                  "malformed standard input exits 2")
        finally:
            sys.stdin = saved
        check(main(["-", "--apply"]) == 64,
              "--apply on standard input without --out is a usage error, "
              "because there is no file to rewrite")

        check(write_json(poly(ccw), path("w.geojson")) is None
              and os.path.exists(path("w.geojson")),
              "write_json writes a document to a path that does not exist yet")
        check(write_json(poly(cw), path("w.geojson")) is None
              and read("w.geojson")["coordinates"][0] == cw,
              "and replaces one that does")

        # A document json cannot serialise fails halfway through the dump. The
        # target must survive it and the temporary file must not.
        names = sorted(os.listdir(tmp))
        try:
            write_json({"type": "Polygon", "coordinates": set()},
                       path("w.geojson"))
            check(False, "an unserialisable document raises out of write_json")
        except TypeError:
            check(True, "an unserialisable document raises out of write_json")
        check(read("w.geojson")["coordinates"][0] == cw,
              "a failed write leaves the previous file intact  "
              "<-- pinned defect")
        check(sorted(os.listdir(tmp)) == names,
              "and leaves no half-written temporary file behind  "
              "<-- pinned defect")
    finally:
        shutil.rmtree(tmp)
    check(not os.path.isdir(tmp),
          "the self-test leaves no temporary directory behind")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
