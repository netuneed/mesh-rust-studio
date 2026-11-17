// SPDX-License-Identifier: GPL-2.0-or-later

extern crate blender_screen_split;

use blender_screen_split::{RustSplitCorners, RustSplitPoint, blender_screen_split_plan};

type Split = unsafe extern "C" fn(i32, i16, i16, i16, i16, i32, i32, f32, i32, f32, f32) -> i16;
type SplitArea =
    unsafe extern "C" fn(i32, i16, i16, i16, i16, i32, i32, f32, i32, f32, f32, u8) -> Observation;

#[repr(C)]
#[derive(Debug, PartialEq, Eq)]
struct Observation {
    split: i16,
    points: [[i16; 2]; 2],
    edges: [[u8; 2]; 5],
    original_corners: [u8; 4],
    new_corners: [u8; 4],
    operations: [u8; 13],
    operation_count: u8,
    returned_new_area: u8,
    new_space_type: u8,
    copied_from_original: u8,
    full_copy: u8,
}

const _: () = {
    use std::mem::{align_of, offset_of, size_of};
    assert!(size_of::<Observation>() == 46 && align_of::<Observation>() == 2);
    assert!(offset_of!(Observation, operations) == 28);
    assert!(offset_of!(Observation, operation_count) == 41);
};

unsafe extern "C" {
    fn upstream_split(
        axis: i32,
        left: i16,
        bottom: i16,
        right: i16,
        top: i16,
        width: i32,
        height: i32,
        scale: f32,
        header: i32,
        pixels: f32,
        factor: f32,
    ) -> i16;
    fn ported_split(
        axis: i32,
        left: i16,
        bottom: i16,
        right: i16,
        top: i16,
        width: i32,
        height: i32,
        scale: f32,
        header: i32,
        pixels: f32,
        factor: f32,
    ) -> i16;
    fn upstream_area_split(
        axis: i32,
        left: i16,
        bottom: i16,
        right: i16,
        top: i16,
        width: i32,
        height: i32,
        scale: f32,
        header: i32,
        pixels: f32,
        factor: f32,
        has_area: u8,
    ) -> Observation;
    fn ported_area_split(
        axis: i32,
        left: i16,
        bottom: i16,
        right: i16,
        top: i16,
        width: i32,
        height: i32,
        scale: f32,
        header: i32,
        pixels: f32,
        factor: f32,
        has_area: u8,
    ) -> Observation;
    fn corrupt_area_split(
        axis: i32,
        left: i16,
        bottom: i16,
        right: i16,
        top: i16,
        width: i32,
        height: i32,
        scale: f32,
        header: i32,
        pixels: f32,
        factor: f32,
        corrupt_field: u8,
    ) -> Observation;
}

fn compare_coordinates() {
    let mut count = 0;
    for axis in 0..2 {
        for (scale, header) in [(0.75, 19), (1.0, 26), (1.5, 39), (2.0, 52)] {
            for pixels in [1.0, 1.5, 2.0, 3.0] {
                let min = if axis == 0 {
                    header
                } else {
                    (29.0 * scale) as i32
                };
                for size in [
                    1,
                    2 * min - 1,
                    2 * min,
                    2 * min + 1,
                    2 * min + 2,
                    201,
                    1349,
                    32000,
                ] {
                    for (before, after) in [(0, 0), (0, 8), (6, 0), (6, 8)] {
                        let end = before + size - 1;
                        let limit = end + 1 + after;
                        let midpoint = (size as f32 / 2.0).floor();
                        for factor in [
                            -0.5,
                            0.0,
                            0.01,
                            0.5,
                            0.99,
                            1.0,
                            1.5,
                            (midpoint - 0.5001) / size as f32,
                            (midpoint - 0.5) / size as f32,
                            (midpoint - 0.4999) / size as f32,
                        ] {
                            let call = |split: Split| unsafe {
                                split(
                                    axis,
                                    before as i16,
                                    before as i16,
                                    end as i16,
                                    end as i16,
                                    limit,
                                    limit,
                                    scale,
                                    header,
                                    pixels,
                                    factor,
                                )
                            };
                            assert_eq!(
                                call(upstream_split),
                                call(ported_split),
                                "axis={axis} scale={scale} header={header} pixels={pixels} \
                                 size={size} edges=({before}, {after}) factor={factor}"
                            );
                            count += 1;
                        }
                    }
                }
            }
        }
    }
    let split = blender_screen_split::blender_screen_split_point;
    assert_eq!(split(0, 140, 0, 141, 29, 1.0, f32::NAN), 0);
    println!("{count} upstream C++ vs patched C++/Rust cases passed; NaN rejected");
}

#[derive(Debug, Clone, Copy)]
struct Case {
    // Blender's eScreenAxis is horizontal=0, vertical=1.
    axis: i32,
    left: i16,
    bottom: i16,
    right: i16,
    top: i16,
    width: i32,
    height: i32,
    scale: f32,
    header: i32,
    pixels: f32,
    factor: f32,
}

impl Case {
    fn observe(self, split: SplitArea, control: u8) -> Observation {
        unsafe {
            split(
                self.axis,
                self.left,
                self.bottom,
                self.right,
                self.top,
                self.width,
                self.height,
                self.scale,
                self.header,
                self.pixels,
                self.factor,
                control,
            )
        }
    }

    fn corners(self) -> RustSplitCorners {
        RustSplitCorners {
            points: [
                RustSplitPoint {
                    x: self.left,
                    y: self.bottom,
                },
                RustSplitPoint {
                    x: self.left,
                    y: self.top,
                },
                RustSplitPoint {
                    x: self.right,
                    y: self.top,
                },
                RustSplitPoint {
                    x: self.right,
                    y: self.bottom,
                },
            ],
        }
    }

    fn limits(self) -> (i16, i16) {
        if self.axis == 0 {
            (self.bottom, self.top)
        } else {
            (self.left, self.right)
        }
    }
}

impl Observation {
    fn verify_rejection(&self, split: i16, operations: &[u8]) {
        assert_eq!(self.split, split);
        assert_eq!(self.operation_count as usize, operations.len());
        assert_eq!(&self.operations[..operations.len()], operations);
        assert_eq!(self.points, [[0; 2]; 2]);
        assert_eq!(self.edges, [[0; 2]; 5]);
        assert_eq!(self.original_corners, [0, 1, 2, 3]);
        assert_eq!(self.new_corners, [0; 4]);
        assert_eq!(self.returned_new_area, 0);
        assert_eq!(self.copied_from_original, 0);
        assert_eq!(self.full_copy, 0);
    }

    fn verify_topology(&self, case: Case) -> bool {
        assert_eq!(self.operation_count, 13);
        assert_eq!(self.operations, [1, 2, 3, 3, 4, 4, 4, 4, 4, 5, 6, 7, 8]);
        assert_eq!(self.returned_new_area, 1);
        assert_eq!(self.new_space_type, 23);
        assert_eq!(self.copied_from_original, 1);
        assert_eq!(self.full_copy, 1);
        let low_corner = if case.axis == 0 { 0 } else { 1 };
        let original_side = u8::from(!self.original_corners.contains(&low_corner));
        assert_eq!(original_side, u8::from(case.factor <= 0.5));

        // Validate the public ABI result in addition to observing the real C++ caller.
        let rust =
            blender_screen_split_plan(case.corners(), 1 - case.axis as u8, self.split, case.factor);
        assert_eq!(rust.valid, 1);
        assert_eq!(rust.points.map(|point| [point.x, point.y]), self.points);
        assert_eq!(rust.edges, self.edges);
        assert_eq!(rust.original_corners, self.original_corners);
        assert_eq!(rust.new_corners, self.new_corners);
        assert_eq!(rust.original_side, original_side);

        let (start, end) = case.limits();
        let size = i32::from(end) - i32::from(start) + 1;
        let ideal = i32::from(start) + (case.factor.clamp(0.0, 1.0) * size as f32).round() as i32;
        i32::from(self.split) != ideal
    }
}

fn compare_topology() {
    let mut accepted = 0;
    let mut rejected = 0;
    let mut constrained = 0;
    let mut constrained_at_half = 0;
    let mut crossed_at_half = 0;
    for axis in 0..2 {
        for (scale, header) in [(0.75, 19), (1.0, 26), (1.5, 39), (2.0, 52)] {
            let minimum = if axis == 0 {
                header
            } else {
                (29.0 * scale) as i32
            };
            for pixels in [1.0, 1.5, 2.0, 3.0] {
                for size in [
                    1,
                    2 * minimum - 1,
                    2 * minimum,
                    2 * minimum + 1,
                    2 * minimum + 2,
                    2 * minimum + 3,
                    231,
                    1349,
                    32000,
                ] {
                    for (before, after) in [(0, 0), (0, 8), (6, 0), (6, 8)] {
                        let end = before + size - 1;
                        let limit = end + 1 + after;
                        for factor in [
                            f32::NEG_INFINITY,
                            0.0,
                            0.01,
                            0.25,
                            0.5_f32.next_down(),
                            0.5,
                            0.5_f32.next_up(),
                            0.75,
                            0.99,
                            1.0,
                            f32::INFINITY,
                        ] {
                            let (left, bottom, right, top, width, height) = if axis == 0 {
                                (11, before, 618, end, 640, limit)
                            } else {
                                (before, 23, end, 448, limit, 471)
                            };
                            let case = Case {
                                axis,
                                left: left as i16,
                                bottom: bottom as i16,
                                right: right as i16,
                                top: top as i16,
                                width,
                                height,
                                scale,
                                header,
                                pixels,
                                factor,
                            };
                            let baseline = case.observe(upstream_area_split, 1);
                            let ported = case.observe(ported_area_split, 1);
                            assert_eq!(baseline, ported, "{case:?}");
                            if baseline.split == 0 {
                                ported.verify_rejection(0, &[1, 2]);
                                rejected += 1;
                            } else {
                                let clamped = ported.verify_topology(case);
                                if clamped {
                                    constrained += 1;
                                    if factor == 0.5 {
                                        constrained_at_half += 1;
                                    }
                                    let (start, end) = case.limits();
                                    let doubled_split = 2 * i32::from(ported.split);
                                    let midpoint = i32::from(start) + i32::from(end);
                                    if (factor > 0.5 && doubled_split < midpoint)
                                        || (factor <= 0.5 && doubled_split > midpoint)
                                    {
                                        crossed_at_half += 1;
                                    }
                                }
                                accepted += 1;
                            }
                        }
                    }
                }
            }
        }
    }
    assert_eq!(accepted + rejected, 12_672);
    assert!(rejected > 0 && constrained > 0 && constrained_at_half > 0 && crossed_at_half > 0);
    println!(
        "{accepted} Split topology matches, {rejected} size rejections, {constrained} constrained coordinates ({constrained_at_half} at factor 0.5, {crossed_at_half} opposite the raw factor)"
    );

    for axis in 0..2 {
        let case = Case {
            axis,
            left: 2,
            bottom: 3,
            right: 394,
            top: 249,
            width: 400,
            height: 260,
            scale: 1.0,
            header: 26,
            pixels: 1.0,
            factor: 0.5,
        };
        let split = case.observe(upstream_area_split, 1).split;
        case.observe(upstream_area_split, 0)
            .verify_rejection(0, &[]);
        case.observe(ported_area_split, 0).verify_rejection(0, &[]);
        Case {
            factor: f32::NAN,
            ..case
        }
        .observe(ported_area_split, 1)
        .verify_rejection(0, &[1, 2]);
        for field in 0..5 {
            case.observe(corrupt_area_split, field)
                .verify_rejection(split, &[1, 2]);
        }
        for (bad_axis, bad_split, bad_factor) in [
            (2, split, 0.5),
            (255, split, 0.5),
            (axis as u8, 0, 0.5),
            (axis as u8, split, f32::NAN),
        ] {
            let plan = blender_screen_split_plan(case.corners(), bad_axis, bad_split, bad_factor);
            assert_eq!(plan.valid, 0);
        }
    }
    println!(
        "Null area, NaN, invalid ABI inputs and five corrupt output fields reject before geometry mutation"
    );
}

fn main() {
    compare_coordinates();
    compare_topology();
}
