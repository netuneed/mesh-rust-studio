// SPDX-FileCopyrightText: 2023 Blender Authors
// SPDX-License-Identifier: GPL-2.0-or-later

mod bmesh_pool;
mod bmesh_quads;
mod curve_ring;
mod deform;
mod edit_mesh;
mod subdiv_edges;
mod subdiv_evaluate;
mod subdiv_topology;

const VERTICAL: u8 = 0;
const HORIZONTAL: u8 = 1;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct RustSplitPoint {
    pub x: i16,
    pub y: i16,
}

#[repr(C)]
pub struct RustSplitCorners {
    pub points: [RustSplitPoint; 4],
}

#[repr(C)]
pub struct RustSplitPlan {
    pub points: [RustSplitPoint; 2],
    pub edges: [[u8; 2]; 5],
    pub original_corners: [u8; 4],
    pub new_corners: [u8; 4],
    pub original_side: u8,
    pub valid: u8,
}

const _: () = {
    use std::mem::{align_of, offset_of, size_of};

    assert!(size_of::<RustSplitPoint>() == 4 && align_of::<RustSplitPoint>() == 2);
    assert!(offset_of!(RustSplitPoint, x) == 0 && offset_of!(RustSplitPoint, y) == 2);
    assert!(size_of::<RustSplitCorners>() == 16 && align_of::<RustSplitCorners>() == 2);
    assert!(offset_of!(RustSplitCorners, points) == 0);
    assert!(size_of::<RustSplitPlan>() == 28 && align_of::<RustSplitPlan>() == 2);
    assert!(offset_of!(RustSplitPlan, points) == 0);
    assert!(offset_of!(RustSplitPlan, edges) == 8);
    assert!(offset_of!(RustSplitPlan, original_corners) == 18);
    assert!(offset_of!(RustSplitPlan, new_corners) == 22);
    assert!(offset_of!(RustSplitPlan, original_side) == 26);
    assert!(offset_of!(RustSplitPlan, valid) == 27);
};

impl RustSplitPlan {
    const INVALID: Self = Self {
        points: [RustSplitPoint { x: 0, y: 0 }; 2],
        edges: [[0; 2]; 5],
        original_corners: [0; 4],
        new_corners: [0; 4],
        original_side: 0,
        valid: 0,
    };
}

/// Plan the topology for Blender v5.3.0's area_split. The caller provides corners
/// in bottom-left, top-left, top-right, bottom-right order and a resolved split coordinate.
/// Vertex indices 0..4 refer to those corners and 4..6 to the new points, in insertion order.
#[unsafe(no_mangle)]
pub extern "C" fn blender_screen_split_plan(
    corners: RustSplitCorners,
    axis: u8,
    split: i16,
    fac: f32,
) -> RustSplitPlan {
    if split == 0 || fac.is_nan() {
        return RustSplitPlan::INVALID;
    }

    let [bottom_left, top_left, _, bottom_right] = corners.points;
    let (points, edges, low, high) = match axis {
        HORIZONTAL => (
            [
                RustSplitPoint {
                    x: bottom_left.x,
                    y: split,
                },
                RustSplitPoint {
                    x: bottom_right.x,
                    y: split,
                },
            ],
            [[0, 4], [4, 1], [2, 5], [5, 3], [4, 5]],
            [0, 4, 5, 3],
            [4, 1, 2, 5],
        ),
        VERTICAL => (
            [
                RustSplitPoint {
                    x: split,
                    y: bottom_left.y,
                },
                RustSplitPoint {
                    x: split,
                    y: top_left.y,
                },
            ],
            [[0, 4], [4, 3], [1, 5], [5, 2], [4, 5]],
            [0, 1, 5, 4],
            [4, 5, 2, 3],
        ),
        _ => return RustSplitPlan::INVALID,
    };

    // The original editor stays on the side chosen by the raw factor, even if the
    // minimum-size adjustment moved the divider across the midpoint.
    let (original_corners, new_corners, original_side) = if fac > 0.5 {
        (low, high, 0)
    } else {
        (high, low, 1)
    };
    if trace_enabled() {
        use std::io::Write;
        let _ = writeln!(
            std::io::stderr().lock(),
            "RUST_SCREEN_SPLIT_PLAN axis={axis} factor={fac} split={split} original_side={original_side}"
        );
    }
    RustSplitPlan {
        points,
        edges,
        original_corners,
        new_corners,
        original_side,
        valid: 1,
    }
}

/// Port of Blender v5.3.0's screen_geom_find_area_split_point.
/// The caller supplies the coordinates and UI minimum for the selected axis.
#[unsafe(no_mangle)]
pub extern "C" fn blender_screen_split_point(
    area_start: i16,
    area_end: i16,
    window_start: i32,
    window_end: i32,
    minimum: i16,
    pixel_size: f32,
    factor: f32,
) -> i16 {
    let result = split_point(
        area_start,
        area_end,
        window_start,
        window_end,
        minimum,
        pixel_size,
        factor,
    );
    if trace_enabled() {
        use std::io::Write;
        let _ = writeln!(
            std::io::stderr().lock(),
            "RUST_SCREEN_SPLIT start={area_start} end={area_end} window_start={window_start} window_end={window_end} minimum={minimum} pixel_size={pixel_size} factor={factor} result={result}"
        );
    }
    result
}

fn trace_enabled() -> bool {
    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ENABLED.get_or_init(|| {
        std::env::var_os("BLENDER_RUST_SPLIT_TRACE").as_deref() == Some(std::ffi::OsStr::new("1"))
    })
}

fn split_point(
    area_start: i16,
    area_end: i16,
    window_start: i32,
    window_end: i32,
    minimum: i16,
    pixel_size: f32,
    factor: f32,
) -> i16 {
    let start = i32::from(area_start);
    let end = i32::from(area_end);
    let size = end - start + 1;
    let mut minimum = i32::from(minimum);
    if size <= 2 * minimum || factor.is_nan() {
        return 0;
    }

    // Blender rounds via floorf(value + 0.5f), then narrows to short.
    let offset = (factor.clamp(0.0, 1.0) * size as f32 + 0.5).floor() as i32 as i16;
    let split = (start + i32::from(offset)) as i16;
    let split = i32::from(split);

    if start > window_start {
        minimum = (minimum as f32 + pixel_size) as i32;
    }
    if end < window_end.saturating_sub(1) {
        minimum = (minimum as f32 + pixel_size) as i32;
    }

    if split - start < minimum {
        (start + minimum) as i16
    } else if end - split < minimum {
        (end - minimum) as i16
    } else {
        split as i16
    }
}
