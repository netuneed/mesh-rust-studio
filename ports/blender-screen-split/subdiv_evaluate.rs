// SPDX-FileCopyrightText: 2015 Pixar
// SPDX-License-Identifier: TOST-1.0

//! QUADS and regular Catmark position and UV evaluation adapted from OpenSubdiv 3.7.0
//! (`opensubdiv/osd/patchBasis.h` and `opensubdiv/osd/cpuEvaluator.cpp`).

use std::mem::{align_of, offset_of, size_of};

#[repr(C)]
pub struct SubdivPositionPatch {
    control: [[f32; 3]; 16],
    scale: f32,
    offset_u: f32,
    offset_v: f32,
    kind: u32,
}

#[repr(C)]
pub struct SubdivUvPatch {
    control: [[f32; 2]; 16],
    scale: f32,
    offset_u: f32,
    offset_v: f32,
    kind: u32,
    boundary: u32,
}

const _: () = {
    assert!(size_of::<SubdivPositionPatch>() == 208 && align_of::<SubdivPositionPatch>() == 4);
    assert!(offset_of!(SubdivPositionPatch, control) == 0);
    assert!(offset_of!(SubdivPositionPatch, scale) == 192);
    assert!(offset_of!(SubdivPositionPatch, offset_u) == 196);
    assert!(offset_of!(SubdivPositionPatch, offset_v) == 200);
    assert!(offset_of!(SubdivPositionPatch, kind) == 204);
    assert!(size_of::<SubdivUvPatch>() == 148 && align_of::<SubdivUvPatch>() == 4);
    assert!(offset_of!(SubdivUvPatch, control) == 0);
    assert!(offset_of!(SubdivUvPatch, scale) == 128);
    assert!(offset_of!(SubdivUvPatch, offset_u) == 132);
    assert!(offset_of!(SubdivUvPatch, offset_v) == 136);
    assert!(offset_of!(SubdivUvPatch, kind) == 140);
    assert!(offset_of!(SubdivUvPatch, boundary) == 144);
};

fn spline(t: f32) -> [f32; 4] {
    let sixth = 1.0f32 / 6.0;
    let square = t * t;
    let cube = t * square;
    [
        sixth * (1.0 - 3.0 * (t - square) - cube),
        sixth * (4.0 - 6.0 * square + 3.0 * cube),
        sixth * (1.0 + 3.0 * (t + square - cube)),
        sixth * cube,
    ]
}

fn accumulate<const WIDTH: usize>(
    control: &[[f32; WIDTH]],
    weights: impl IntoIterator<Item = f32>,
) -> [f32; WIDTH] {
    let mut value = [0.0; WIDTH];
    for (point, weight) in control.iter().zip(weights) {
        for (component, coordinate) in value.iter_mut().zip(point) {
            *component += coordinate * weight;
        }
    }
    value
}

fn adjust_boundary_weights(boundary: u32, weights: &mut [f32; 16]) {
    // The order matters when two boundaries affect the same weight.
    for (mask, start, step, subtract, add) in [
        (1, 0, 1, 8, 4),
        (2, 3, 4, 1, 2),
        (4, 12, 1, 4, 8),
        (8, 0, 4, 2, 1),
    ] {
        if boundary & mask != 0 {
            for i in 0..4 {
                let weight = weights[start + i * step];
                weights[subtract + i * step] -= weight;
                weights[add + i * step] += weight * 2.0;
                weights[start + i * step] = 0.0;
            }
        }
    }
}

struct PatchParameters {
    scale: f32,
    offset_u: f32,
    offset_v: f32,
    kind: u32,
    boundary: u32,
}

// The caller supplies the same storage guarantees as the FFI exports below.
unsafe fn evaluate_batch<const WIDTH: usize>(
    control: &[[f32; WIDTH]; 16],
    param: PatchParameters,
    uv: *const [f32; 2],
    count: usize,
    output: *mut [f32; WIDTH],
) -> u8 {
    if !(2..=128).contains(&count)
        || param.kind > 1
        || param.boundary > 31
        || !param.scale.is_finite()
        || param.scale <= 0.0
        || !param.offset_u.is_finite()
        || !param.offset_v.is_finite()
        || uv.is_null()
        || output.is_null()
        || !uv.is_aligned()
        || !output.is_aligned()
    {
        return 0;
    }
    let uv_start = uv.addr();
    let output_start = output.addr();
    let Some(uv_end) = uv_start.checked_add(count * size_of::<[f32; 2]>()) else {
        return 0;
    };
    let Some(output_end) = output_start.checked_add(count * size_of::<[f32; WIDTH]>()) else {
        return 0;
    };
    if uv_start < output_end && output_start < uv_end {
        return 0;
    }

    // SAFETY: A bounded stack input and a disjoint Mesh positions range are lent by C++.
    let uv = unsafe { std::slice::from_raw_parts(uv, count) };
    if param.kind == 0 {
        for (index, &[u, v]) in uv.iter().enumerate() {
            let u = u * param.scale - param.offset_u;
            let v = v * param.scale - param.offset_v;
            let position = accumulate(
                &control[..4],
                [(1.0 - u) * (1.0 - v), u * (1.0 - v), u * v, (1.0 - u) * v],
            );
            // SAFETY: `index` is bounded by count. A mesh slice can be uninitialized here.
            unsafe { output.add(index).write(position) };
        }
    } else {
        let mut previous_v = None;
        let mut v_weights = [0.0; 4];
        for (index, &[u, v]) in uv.iter().enumerate() {
            if previous_v != Some(v.to_bits()) {
                v_weights = spline(v * param.scale - param.offset_v);
                previous_v = Some(v.to_bits());
            }
            let u_weights = spline(u * param.scale - param.offset_u);
            let basis = v_weights.into_iter().flat_map(|y| u_weights.map(|x| x * y));
            let position = if param.boundary == 0 {
                accumulate(control, basis)
            } else {
                let mut weights = [0.0; 16];
                for (weight, value) in weights.iter_mut().zip(basis) {
                    *weight = value;
                }
                adjust_boundary_weights(param.boundary, &mut weights);
                accumulate(control, weights)
            };
            // SAFETY: `index` is bounded by count. A mesh slice can be uninitialized here.
            unsafe { output.add(index).write(position) };
        }
    }
    1
}

/// # Safety
/// For accepted pointers, `uv` must point to `count` initialized pairs and `positions` to
/// `count` writable triples. These ranges must be live for the call and not alias.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn blender_rust_subdiv_position_batch(
    patch: SubdivPositionPatch,
    uv: *const [f32; 2],
    count: usize,
    positions: *mut [f32; 3],
) -> u8 {
    let param = PatchParameters {
        scale: patch.scale,
        offset_u: patch.offset_u,
        offset_v: patch.offset_v,
        kind: patch.kind,
        boundary: 0,
    };
    unsafe { evaluate_batch(&patch.control, param, uv, count, positions) }
}

/// # Safety
/// For accepted pointers, `uv` must point to `count` initialized pairs and `output` to
/// `count` writable pairs. These ranges must be live for the call and not alias.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn blender_rust_subdiv_uv_batch(
    patch: SubdivUvPatch,
    uv: *const [f32; 2],
    count: usize,
    output: *mut [f32; 2],
) -> u8 {
    let param = PatchParameters {
        scale: patch.scale,
        offset_u: patch.offset_u,
        offset_v: patch.offset_v,
        kind: patch.kind,
        boundary: patch.boundary,
    };
    unsafe { evaluate_batch(&patch.control, param, uv, count, output) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn patch() -> SubdivPositionPatch {
        SubdivPositionPatch {
            control: [[2.0, 3.0, -1.0]; 16],
            scale: 1.0,
            offset_u: 0.0,
            offset_v: 0.0,
            kind: 0,
        }
    }

    #[test]
    fn accepts_two_samples_and_rejects_bad_arguments_without_writing() {
        let input = [[0.25, 0.5], [0.75, 0.5]];
        let mut output = [[5.0; 3]; 2];
        let output_ptr = output.as_mut_ptr();
        unsafe {
            assert_eq!(
                blender_rust_subdiv_position_batch(patch(), input.as_ptr(), 1, output_ptr),
                0
            );
            assert_eq!(
                blender_rust_subdiv_position_batch(patch(), input.as_ptr(), 129, output_ptr),
                0
            );
            assert_eq!(
                blender_rust_subdiv_position_batch(
                    patch(),
                    input.as_ptr(),
                    2,
                    std::ptr::null_mut()
                ),
                0
            );
            assert_eq!(
                blender_rust_subdiv_position_batch(patch(), output_ptr.cast(), 2, output_ptr),
                0
            );
            let mut bad_patch = patch();
            bad_patch.kind = 2;
            assert_eq!(
                blender_rust_subdiv_position_batch(bad_patch, input.as_ptr(), 2, output_ptr),
                0
            );
            assert_eq!(output, [[5.0; 3]; 2]);
            assert_eq!(
                blender_rust_subdiv_position_batch(patch(), input.as_ptr(), 2, output_ptr),
                1
            );
        }
        assert_eq!(output, [[2.0, 3.0, -1.0]; 2]);
    }
}
