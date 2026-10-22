//! Real towns from OpenStreetMap (`assets/osm/<name>.ron`, written by `tools/osm/extract.py`):
//! a town's footprints and streets replace its generated street grid and buildings. Positions
//! are metres around the town centre at real scale (x east, y north): the theater squeezes
//! the distances between towns, not the towns.
//!
//! Each footprint becomes a building whose rectangle is the footprint's smallest enclosing
//! rectangle (streaming, clearances, ruins and the pitched roofs of four-sided footprints);
//! the outline itself, with its courtyards, is kept in `Network::outlines` for the walls and
//! the flat roof. Houses and other small buildings are drawn only near the viewpoint
//! (`Tier::Small`). The pilot can fly into the buildings ([`Network::osm_solid`]).

use bevy::log::info;
use bevy::math::DVec2;
use serde::Deserialize;

use super::content::roof_height;
use super::geom::{bridge_holes, triangulate};
use super::ground::Ground;
use super::network::{Building, Kind, Network, Rng, Road, RoadKind, Roof, dist_to_segment, hash};
use super::style::Style;

#[derive(Deserialize)]
pub struct OsmTown {
    pub name: String,
    pub buildings: Vec<OsmBuilding>,
    #[serde(default)]
    pub roads: Vec<OsmRoad>,
}

#[derive(Deserialize)]
pub struct OsmBuilding {
    pub kind: Kind,
    /// Wall height (metres).
    pub h: f32,
    pub roof: Roof,
    pub pts: Vec<(f64, f64)>,
    /// Courtyards.
    #[serde(default)]
    pub holes: Vec<Vec<(f64, f64)>>,
}

#[derive(Deserialize)]
pub struct OsmRoad {
    pub width: f64,
    pub pts: Vec<(f64, f64)>,
}

/// A real footprint in ground metres: the outline counter-clockwise seen from above, the
/// courtyards clockwise, and the flat roof's triangles (counter-clockwise).
pub struct Outline {
    pub outer: Vec<DVec2>,
    pub holes: Vec<Vec<DVec2>>,
    pub roof: Vec<[DVec2; 3]>,
}

impl Outline {
    /// From an outline and courtyards wound either way.
    pub fn new(mut outer: Vec<DVec2>, mut holes: Vec<Vec<DVec2>>) -> Self {
        if area(&outer) < 0.0 {
            outer.reverse();
        }
        for h in &mut holes {
            if area(h) > 0.0 {
                h.reverse();
            }
        }
        let poly = if holes.is_empty() { outer.clone() } else { bridge_holes(&outer, &holes) };
        let roof = triangulate(&poly).into_iter().map(|t| t.map(|i| poly[i])).collect();
        Self { outer, holes, roof }
    }

    /// Ground area (courtyards left out).
    pub fn area(&self) -> f64 {
        area(&self.outer) + self.holes.iter().map(|h| area(h)).sum::<f64>()
    }

    pub fn contains(&self, p: DVec2) -> bool {
        inside(&self.outer, p) && !self.holes.iter().any(|h| inside(h, p))
    }

    /// A point on the roof (a beacon's): the middle of its largest triangle.
    pub fn roof_point(&self) -> DVec2 {
        let size = |t: &[DVec2; 3]| (t[1] - t[0]).perp_dot(t[2] - t[0]);
        self.roof
            .iter()
            .max_by(|a, b| size(a).total_cmp(&size(b)))
            .map_or(self.outer[0], |t| (t[0] + t[1] + t[2]) / 3.0)
    }
}

/// Footprints under this area (square metres), and all houses and sheds, are drawn only near
/// the viewpoint.
const SMALL_AREA: f64 = 150.0;
/// Street sampling step (metres) for the open land test.
const STREET_STEP: f64 = 20.0;

pub fn load(name: &str) -> OsmTown {
    crate::asset_path::load_ron(&format!("osm/{name}.ron"))
}

/// Signed area (positive counter-clockwise seen from above).
fn area(pts: &[DVec2]) -> f64 {
    let n = pts.len();
    (0..n).map(|i| pts[i].perp_dot(pts[(i + 1) % n])).sum::<f64>() * 0.5
}

/// Point in polygon (even-odd rule).
fn inside(pts: &[DVec2], p: DVec2) -> bool {
    let mut hit = false;
    for i in 0..pts.len() {
        let (a, b) = (pts[i], pts[(i + 1) % pts.len()]);
        if (a.y > p.y) != (b.y > p.y) && p.x < a.x + (p.y - a.y) * (b.x - a.x) / (b.y - a.y) {
            hit = !hit;
        }
    }
    hit
}

/// Smallest rectangle around a polygon with a side along one of its edges: centre, unit
/// direction of the length, length and width.
fn enclosing_rect(pts: &[DVec2]) -> (DVec2, DVec2, f64, f64) {
    let mut best = (f64::MAX, pts[0], DVec2::X, 0.0, 0.0);
    for i in 0..pts.len() {
        let u = (pts[(i + 1) % pts.len()] - pts[i]).normalize_or_zero();
        if u == DVec2::ZERO {
            continue;
        }
        let v = u.perp();
        let (mut lo, mut hi) = (DVec2::splat(f64::MAX), DVec2::splat(f64::MIN));
        for p in pts {
            let q = DVec2::new(p.dot(u), p.dot(v));
            lo = lo.min(q);
            hi = hi.max(q);
        }
        let size = hi - lo;
        if size.x * size.y < best.0 {
            let m = (lo + hi) * 0.5;
            let c = u * m.x + v * m.y;
            best = if size.x >= size.y {
                (size.x * size.y, c, u, size.x, size.y)
            } else {
                (size.x * size.y, c, v, size.y, size.x)
            };
        }
    }
    (best.1, best.2, best.3, best.4)
}

/// Corners of a building's rectangle.
fn corners(b: &Building) -> [DVec2; 4] {
    let (u, v) = (b.dir * (b.l as f64 * 0.5), b.dir.perp() * (b.w as f64 * 0.5));
    [b.p - u - v, b.p + u - v, b.p + u + v, b.p - u + v]
}

/// The stretches of a street over open land (no water, concrete or theater objects, like the
/// generated streets): a segment that is not goes whole.
fn open_runs(ground: &Ground, pts: &[DVec2]) -> Vec<Vec<DVec2>> {
    let open = |a: DVec2, b: DVec2| {
        let n = (a.distance(b) / STREET_STEP).ceil().max(1.0) as usize;
        (0..=n).all(|k| ground.is_free(a.lerp(b, k as f64 / n as f64), 4.0))
    };
    let (mut runs, mut run) = (Vec::new(), Vec::new());
    for w in pts.windows(2) {
        if open(w[0], w[1]) {
            if run.is_empty() {
                run.push(w[0]);
            }
            run.push(w[1]);
        } else if !run.is_empty() {
            runs.push(std::mem::take(&mut run));
        }
    }
    if !run.is_empty() {
        runs.push(run);
    }
    runs
}

impl Network {
    /// A real town's streets, in place of the street grid.
    pub(super) fn osm_roads(&mut self, t: usize, osm: &OsmTown, ground: &Ground) {
        let (c, first) = (self.towns[t].c, self.roads.len());
        for r in &osm.roads {
            let pts: Vec<DVec2> = r.pts.iter().map(|&(x, y)| c + DVec2::new(x, y)).collect();
            for run in open_runs(ground, &pts) {
                self.roads.push(Road::new(run, RoadKind::Street, r.width));
            }
        }
        info!(
            "scenery: {} from OpenStreetMap, {} streets in {} stretches over open land",
            osm.name,
            osm.roads.len(),
            self.roads.len() - first
        );
    }

    /// Whether a building's rectangle reaches a highway strip (the theater's road lines run
    /// through the real towns too).
    fn on_highway(&self, b: &Building) -> bool {
        let (p, r) = (b.p, b.w as f64 * 0.5);
        let d = DVec2::splat(r + 10.0);
        self.segment_index.around(p - d, p + d).any(|i| {
            let (road, s) = self.segments[i as usize];
            let road = &self.roads[road as usize];
            road.kind == RoadKind::Highway
                && dist_to_segment(p, road.pts[s as usize], road.pts[s as usize + 1])
                    < road.width * 0.5 + r
        })
    }

    /// A real town's buildings, in place of the generated lots, clear of water, concrete, the
    /// theater's own objects and its highways; the town's ruined share of them in ruins.
    pub(super) fn osm_buildings(
        &mut self,
        t: usize,
        osm: &OsmTown,
        ground: &Ground,
        style: &Style,
    ) {
        let (c, ruined) = (self.towns[t].c, self.towns[t].ruined);
        let at = |pts: &[(f64, f64)]| -> Vec<DVec2> {
            pts.iter().map(|&(x, y)| c + DVec2::new(x, y)).collect()
        };
        let mut rng = Rng::new(hash(style.seed, t as i64, 0, 7));
        let first = self.buildings.len() as u32;
        let (mut blocked, mut near) = (0, 0);
        for ob in osm.buildings.iter().filter(|ob| ob.pts.len() >= 3) {
            let holes = ob.holes.iter().filter(|h| h.len() >= 3).map(|h| at(h)).collect();
            let o = Outline::new(at(&ob.pts), holes);
            let (p, dir, l, w) = enclosing_rect(&o.outer);
            let mut b = style.footprint(&mut rng, ob.kind, ob.roof, ob.h);
            (b.p, b.dir, b.l, b.w) = (p, dir, l as f32, w as f32);
            if !ground.is_free(p, 2.0) || self.on_highway(&b) {
                blocked += 1;
                continue;
            }
            // Small by what it was, also once in ruins.
            let id = self.buildings.len() as u32;
            if matches!(b.kind, Kind::House | Kind::Shed) || o.area() < SMALL_AREA {
                self.near_only.insert(id);
                near += 1;
            }
            if b.kind != Kind::Ruin && rng.chance(ruined) {
                b.kind = Kind::Ruin;
            }
            if b.kind == Kind::Ruin {
                b.h = b.h.min(18.0);
            }
            // Ruins and pitched roofs are built on the rectangle; churches never are, as the
            // generated ones' bell tower would stand outside the real footprint.
            let pitched = o.outer.len() == 4
                && o.holes.is_empty()
                && b.roof != Roof::Flat
                && b.kind != Kind::Church;
            let on_rect = b.kind == Kind::Ruin || pitched;
            // In the index by its real extent (a turned rectangle reaches past its radius).
            let rect = corners(&b);
            let extent: &[DVec2] = if on_rect { &rect } else { &o.outer };
            let (min, max) = extent.iter().fold((p, p), |(lo, hi), &q| (lo.min(q), hi.max(q)));
            if !on_rect {
                b.roof = Roof::Flat;
                self.outlines.insert(id, o);
            }
            self.insert_building(b, min, max);
        }
        self.osm.push(first..self.buildings.len() as u32);
        info!(
            "scenery: {} from OpenStreetMap, {} buildings ({blocked} on water, concrete, the \
             theater's objects or highways left out, {near} drawn only near the viewpoint)",
            osm.name,
            self.buildings.len() as u32 - first,
        );
    }

    /// Whether a point (ground metres, metres above the ground) is inside a building of the
    /// real towns.
    pub fn osm_solid(&self, p: DVec2, alt: f64) -> bool {
        !self.osm.is_empty()
            && self.building_index.around(p, p).any(|i| {
                let b = &self.buildings[i as usize];
                if !self.osm.iter().any(|r| r.contains(&i)) {
                    return false;
                }
                let top = if b.kind == Kind::Ruin { b.h * 0.5 } else { b.h + roof_height(b) };
                if alt > top as f64 {
                    return false;
                }
                match self.outlines.get(&i) {
                    Some(o) => o.contains(p),
                    None => {
                        let d = p - b.p;
                        d.dot(b.dir).abs() <= b.l as f64 * 0.5
                            && d.dot(b.dir.perp()).abs() <= b.w as f64 * 0.5
                    }
                }
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn square(a: f64, b: f64, at: DVec2) -> Vec<DVec2> {
        [(a, a), (b, a), (b, b), (a, b)].iter().map(|&(x, y)| at + DVec2::new(x, y)).collect()
    }

    #[test]
    fn outline_orientation_and_courtyard() {
        // Both rings given counter-clockwise: the courtyard is turned round.
        let o = Outline::new(square(0.0, 30.0, DVec2::ZERO), vec![square(10.0, 20.0, DVec2::ZERO)]);
        assert!(area(&o.outer) > 0.0 && area(&o.holes[0]) < 0.0);
        assert!((o.area() - 800.0).abs() < 1e-9);
        let roof: f64 = o.roof.iter().map(|t| (t[1] - t[0]).perp_dot(t[2] - t[0]) * 0.5).sum();
        assert!((roof - 800.0).abs() < 1e-6, "{roof}");
        assert!(o.contains(DVec2::new(5.0, 15.0)));
        assert!(!o.contains(DVec2::new(15.0, 15.0)));
        assert!(!o.contains(DVec2::new(35.0, 15.0)));
        assert!(o.contains(o.roof_point()));
    }

    #[test]
    fn flying_into_real_buildings() {
        let block = |p: DVec2, dir: DVec2, roof: Roof| Building {
            p,
            dir,
            kind: Kind::Apartment,
            l: 20.0,
            w: 10.0,
            h: 10.0,
            roof,
            wall: 0xE1,
            roof_colour: 0xE7,
            seed: 0,
        };
        let mut n = Network::default();
        // A generated building (not solid); real ones: on its rectangle with a gable roof, a
        // 30 m square with a courtyard, and a rectangle turned 45 degrees across index cells.
        n.add_building(block(DVec2::ZERO, DVec2::X, Roof::Flat));
        n.add_building(block(DVec2::new(100.0, 0.0), DVec2::X, Roof::Gable));
        let o = Outline::new(
            square(0.0, 30.0, DVec2::new(200.0, 0.0)),
            vec![square(10.0, 20.0, DVec2::new(200.0, 0.0))],
        );
        n.insert_building(
            block(DVec2::new(215.0, 15.0), DVec2::X, Roof::Flat),
            DVec2::new(200.0, 0.0),
            DVec2::new(230.0, 30.0),
        );
        n.outlines.insert(2, o);
        // Its radius (10 m) stops short of the next index cell (x = 500), its corner does not.
        let turned = block(
            DVec2::new(489.7, 100.0),
            DVec2::from_angle(std::f64::consts::FRAC_PI_4),
            Roof::Flat,
        );
        let c = corners(&turned);
        let (min, max) = c.iter().fold((turned.p, turned.p), |(lo, hi), &q| (lo.min(q), hi.max(q)));
        assert!(turned.p.x + turned.radius() < 500.0 && c[1].x > 500.0);
        n.insert_building(turned, min, max);
        n.osm.push(1..4);
        assert!(!n.osm_solid(DVec2::ZERO, 5.0));
        assert!(n.osm_solid(DVec2::new(100.0, 0.0), 5.0));
        // Under the ridge (10 m walls + 4.2 m), not above it nor beside the walls.
        assert!(n.osm_solid(DVec2::new(100.0, 0.0), 13.0));
        assert!(!n.osm_solid(DVec2::new(100.0, 0.0), 15.0));
        assert!(!n.osm_solid(DVec2::new(100.0, 6.0), 5.0));
        assert!(n.osm_solid(DVec2::new(205.0, 15.0), 5.0));
        assert!(!n.osm_solid(DVec2::new(215.0, 15.0), 5.0), "the courtyard is open");
        // Just inside that corner.
        let near_corner = c[1] + (turned.p - c[1]) * 0.01;
        assert!(near_corner.x > 500.0 && n.osm_solid(near_corner, 5.0), "{near_corner}");
    }

    #[test]
    fn enclosing_rect_of_a_turned_rectangle() {
        let (u, v) = (DVec2::from_angle(0.5), DVec2::from_angle(0.5).perp());
        let pts: Vec<DVec2> = [(-20.0, -5.0), (20.0, -5.0), (20.0, 5.0), (-20.0, 5.0)]
            .iter()
            .map(|&(x, y)| DVec2::new(100.0, 50.0) + u * x + v * y)
            .collect();
        let (c, dir, l, w) = enclosing_rect(&pts);
        assert!(c.distance(DVec2::new(100.0, 50.0)) < 1e-9);
        assert!((dir.dot(u).abs() - 1.0).abs() < 1e-9);
        assert!((l - 40.0).abs() < 1e-9 && (w - 10.0).abs() < 1e-9);
    }
}
