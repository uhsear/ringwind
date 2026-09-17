# ringwind

Name every GeoJSON ring wound against the convention you declared, exit non-zero, and never
rewind one without `--apply`.

A county service area is exported from a geodatabase to GeoJSON and loaded into a spherical
geometry engine. Every viewer on the way there draws it correctly: the web map, the desktop
preview, the pull request diff. The engine draws it correctly too.

The geofence built on it then admits every address outside the county and rejects every address
inside it. Esri winds an exterior ring clockwise, RFC 7946 asks for counter-clockwise, and a
spherical engine reads a clockwise exterior as its own complement: the polygon it computes is
everything on the planet except the county. A point-in-polygon test against that complement is not
random. It is exactly inverted, on every row, which is why it reads as working until somebody
checks a specific address by hand.

Nothing reports it. The file is valid GeoJSON, every coordinate is right, and the ring closes.

```
$ python ringwind.py --self-test
ringwind self-test: no file, no network, no credentials
--------------------------------------------------------------------
PASS  a counter-clockwise unit square is not clockwise
PASS  a clockwise unit square is clockwise
...
PASS  three collinear points give a shoelace sum of exactly 0
PASS  a > 0 test calls that collinear ring counter-clockwise, which reads as a correct exterior  <-- pinned defect
PASS  classify_ring reports DEGENERATE instead of passing it  <-- pinned defect
PASS  a self-cancelling bow tie also sums to exactly 0
...
PASS  a bare NaN in a GeoJSON coordinate parses, it does not raise
PASS  a > 0 test calls that NaN ring counter-clockwise, which reads as a correct exterior  <-- pinned defect
PASS  classify_ring reports the NaN ring DEGENERATE instead of clean  <-- pinned defect
PASS  a NaN ring is never rewound: reversing it measures nothing  <-- pinned defect
...
PASS  a two-vertex ring is rejected, not scored
PASS  an already-closed ring is returned unchanged, not closed twice
PASS  the closing edge is half the sum on this triangle, so scoring an unclosed ring would halve it  <-- pinned defect
...
PASS  a bare string where a geometry belongs raises ValueError, not AttributeError  <-- pinned defect
...
PASS  two separate counter-clockwise parts flattened into one ring list measure [False, True]: one exterior and one hole  <-- pinned defect
PASS  the flattened list therefore reports a violation on a ring that is wound correctly for the part it really belongs to
PASS  --esri reports every ring of that same polygon  <-- pinned defect
PASS  the Esri-wound polygon is clean under --esri
...
PASS  a degenerate ring is never rewound: reversing it fixes nothing  <-- pinned defect
PASS  an unclosed ring is closed as it is rewound  <-- pinned defect
...
PASS  a UTF-8 BOM does not stop the file parsing  <-- pinned defect
...
PASS  an Esri-wound file fails under the shipped default
PASS  setting DEFAULT_EXTERIOR_CW makes Esri the convention for a run with no flag at all  <-- pinned defect
PASS  and the RFC-wound file then fails, so the constant really decides the verdict
...
PASS  a file holding no polygon ring exits 2, rather than passing
PASS  a real file carrying a NaN coordinate exits 1 and says DEGENERATE  <-- pinned defect
...
PASS  --out without --apply says out loud that it wrote nothing, which is the line the README promises  <-- pinned defect
PASS  --out without --apply writes nothing at all  <-- pinned defect
...
PASS  the atomic write leaves no .tmp file behind  <-- pinned defect
PASS  a document holding a degenerate ring exits 1 under --apply
PASS  and is not written at all, rather than half rewound  <-- pinned defect
...
PASS  a failed write leaves the previous file intact  <-- pinned defect
PASS  and leaves no half-written temporary file behind  <-- pinned defect
PASS  the self-test leaves no temporary directory behind
--------------------------------------------------------------------
141 assertions, 0 failed
```

## Requirements

Python 3.9 or later. Standard library only: `argparse`, `collections`, `io`, `json`, `math`,
`os`, `shutil`, `sys` and `tempfile`. No `arcpy`, no `shapely`, no `geojson-rewind`, no
network and no credentials. It runs the same on ArcGIS Pro's Python and on a plain
`python3`.

```
git clone https://github.com/uhsear/ringwind.git
python ringwind.py --self-test
```

## Usage

Check a file. Nothing is written.

```
python ringwind.py boundary.geojson
python ringwind.py boundary.geojson --esri
cat boundary.geojson | python ringwind.py -
```

A real run against a two-part boundary exported from a geodatabase, one part carrying a hole:

```
$ python ringwind.py boundary.geojson
ringwind: boundary.geojson
convention: RFC 7946 (exterior counter-clockwise, holes clockwise)
scanned: 1 feature(s), 2 part(s), 3 ring(s)

feature 0 part 0 ring 0 (exterior): WINDING
    wound clockwise; this exterior must be counter-clockwise
feature 0 part 1 ring 0 (exterior): WINDING
    wound clockwise; this exterior must be counter-clockwise

Check only. Nothing was written. Re-run with --apply to rewind 2 ring(s).
FAIL: 2 ring(s) do not match the convention.
```

Rewind it. `--apply` writes; without it, `--out` is ignored and says so.

```
$ python ringwind.py boundary.geojson --out boundary_rfc.geojson --apply
...
rewound 2 ring(s) and wrote boundary_rfc.geojson
FAIL: 2 ring(s) did not match the convention.

$ python ringwind.py boundary_rfc.geojson
ringwind: boundary_rfc.geojson
convention: RFC 7946 (exterior counter-clockwise, holes clockwise)
scanned: 1 feature(s), 2 part(s), 3 ring(s)

VERDICT: CLEAN
```

A rewound run still exits 1. The file was wrong when the job read it, and a scheduled job that
rewinds its own input must still tell you that its upstream export is producing bad rings.

| Flag | Default | What it does |
|---|---|---|
| `GEOJSON` | none | File to check, or `-` for standard input. Required. |
| `--esri` | off | Expect exterior clockwise and holes counter-clockwise, instead of RFC 7946. |
| `--out` | none | Where a rewound document is written. Without it, `--apply` rewrites the input. |
| `--apply` | off | Rewind the wrongly wound rings and write. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

One setting is not a flag. `DEFAULT_EXTERIOR_CW` at the top of the file decides the convention for
a run with no `--esri`. It ships `False`, which is RFC 7946. A shop whose exports are all meant to
stay in Esri's direction sets it `True` once, and then a bare run expects Esri winding.

Exit codes: 0 every ring matches the convention, 1 at least one ring does not, 2 the input could
not be read or holds no polygon, 64 usage error.

## What it checks

- **The winding of every ring, against a convention you name.** Ring 0 of a part is the exterior
  and takes the declared direction. Every later ring is a hole and takes the opposite one. One
  flag decides both, so a file cannot be half converted.
- **Degenerate rings, before winding.** A ring whose shoelace sum is exactly zero has no winding.
  Three collinear points give 0. So does a self-cancelling bow tie. The usual test is `sum > 0`,
  which answers False for both and reports them as correct counter-clockwise exteriors. This
  reports `DEGENERATE` and exits non-zero, and refuses to rewind them, because reversing a ring
  with no direction changes the file and fixes nothing.
- **Non-finite coordinates, which land in the same trap.** Python's `json` module parses bare
  `NaN` and `Infinity`, so a file can be valid JSON and still hold a coordinate that is not a
  number. Every comparison against a `NaN` sum is False, so `sum > 0` again answers False and the
  ring reads as a correct exterior. This reports `DEGENERATE` and names the coordinate.
- **Rings too short to score.** A ring of fewer than four closed positions encloses no area. It is
  reported as `SHORT_RING` and is never given a winding verdict.
- **The address of each finding.** Every finding names a feature index, a part index and a ring
  index. On a MultiPolygon of thirty parts, a message naming only the file is not actionable.
- **Rings that are not closed.** A ring is closed before it is scored, because the shoelace sum
  walks the edges between consecutive positions and an unclosed ring is short one edge. A ring
  that already closes is not closed twice.
- **That it looked at something.** A file holding no polygon ring exits 2. A clean verdict over
  nothing is indistinguishable from a clean verdict over a boundary, which is the more dangerous
  of the two answers.

## Why not geojson-rewind or shapely.orient

Both do the rewinding well, and both are one call. `geojson-rewind` takes a whole document and
returns it wound the RFC way. `shapely.orient` sets the sign on a polygon you already hold. If
your job is to fix a file, use one of them.

Neither one tells you a file was wrong. They return corrected output and report nothing, so a
pipeline that pipes every export through one can never fail on winding, and never learns which
upstream tool produces the bad rings. `shapely.orient` also silently repairs the degenerate case
instead of naming it, because a zero-area ring reversed is still a zero-area ring.

The gap is the refusal, and the address. This reads, names the ring by feature, part and index,
exits 1, and rewinds only when you ask.

## Limits

- Plane geometry. The shoelace sum treats lon/lat as a flat plane. That is correct for the sign of
  a county-sized ring and wrong for a ring that crosses the antimeridian or encloses a pole.
- It has no opinion about which convention is right. `--esri` exists because a file staying inside
  an Esri stack is correct in Esri's direction, and declaring the convention is the operator's job.
- Ring 0 is assumed to be the exterior. GeoJSON says it is. A geodatabase export that lists a hole
  first produces a correct report about the wrong rings, and nothing here can detect that, because
  detecting it needs containment rather than winding.
- Nested holes are not modelled. Every ring after the first is a hole of ring 0, which is what
  GeoJSON allows. A polygon inside a hole belongs in a separate part.
- `--apply` rewrites the whole document compactly, with no spaces and no indentation. Comments do
  not exist in JSON, but key order and formatting are not preserved, so do not run it over a file
  a human maintains by hand.
- Self-intersecting rings other than the exactly-cancelling case are given a winding verdict on
  their net signed area. Winding is not a validity check. Use a topology checker for that.
- The whole document is read into memory. A county boundary is fine; a national parcel layer is
  not.
- A feature carrying no polygon is skipped and counted, not reported. A winding checker has
  nothing to say about a point or a line.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [geocodesift](https://github.com/uhsear/geocodesift) - the containment test this winding breaks
- [gdbfence](https://github.com/uhsear/gdbfence) - keep the geodatabase that produced it out of git
