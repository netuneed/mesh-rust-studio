// SPDX-FileCopyrightText: 2001-2002 NaN Holding BV. All rights reserved.
// SPDX-License-Identifier: GPL-2.0-or-later

//! Position-only dual-quaternion deformation from Blender 5.3.0.

#[repr(C)]
pub struct ArmatureDualQuaternion {
    pub rotation: [f32; 4],
    pub translation: [f32; 4],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct ArmaturePoint {
    pub x: f32,
    pub y: f32,
    pub z: f32,
}

const _: () = {
    use std::mem::{align_of, offset_of, size_of};

    assert!(size_of::<ArmatureDualQuaternion>() == 32 && align_of::<ArmatureDualQuaternion>() == 4);
    assert!(offset_of!(ArmatureDualQuaternion, rotation) == 0);
    assert!(offset_of!(ArmatureDualQuaternion, translation) == 16);
    assert!(size_of::<ArmaturePoint>() == 12 && align_of::<ArmaturePoint>() == 4);
    assert!(offset_of!(ArmaturePoint, x) == 0);
    assert!(offset_of!(ArmaturePoint, y) == 4);
    assert!(offset_of!(ArmaturePoint, z) == 8);
};

/// Finalize a position-only mixer after Blender incorporates scale at the pivot.
/// The caller applies Blender's contribution threshold and retains object-space transforms.
#[unsafe(no_mangle)]
pub extern "C" fn blender_rust_armature_dq_delta(
    dq: ArmatureDualQuaternion,
    co: ArmaturePoint,
    total: f32,
    armature_weight: f32,
) -> ArmaturePoint {
    let scale = 1.0f32 / total;
    let [w, x, y, z] = dq.rotation.map(|component| component * scale);
    let [t0, t1, t2, t3] = dq.translation.map(|component| component * scale);

    // The order of sums matches normalize_dq, mul_v3m3_dq and mul_v3_m3v3.
    let length_squared = w * w + x * x + y * y + z * z;
    let inverse_length_squared = if length_squared > 0.0 {
        1.0 / length_squared
    } else {
        length_squared
    };
    let m00 = w * w + x * x - y * y - z * z;
    let m10 = 2.0 * (x * y - w * z);
    let m20 = 2.0 * (x * z + w * y);
    let m01 = 2.0 * (x * y + w * z);
    let m11 = w * w + y * y - x * x - z * z;
    let m21 = 2.0 * (y * z - w * x);
    let m02 = 2.0 * (x * z - w * y);
    let m12 = 2.0 * (y * z + w * x);
    let m22 = w * w + z * z - x * x - y * y;

    let dx = 2.0 * (-t0 * x + w * t1 - t2 * z + y * t3);
    let dy = 2.0 * (-t0 * y + t1 * z - x * t3 + w * t2);
    let dz = 2.0 * (-t0 * z + x * t2 + w * t3 - t1 * y);
    let rotated_x = m00 * co.x + m10 * co.y + m20 * co.z;
    let rotated_y = m01 * co.x + m11 * co.y + m21 * co.z;
    let rotated_z = m02 * co.x + m12 * co.y + m22 * co.z;

    ArmaturePoint {
        x: ((rotated_x + dx) * inverse_length_squared - co.x) * armature_weight,
        y: ((rotated_y + dy) * inverse_length_squared - co.y) * armature_weight,
        z: ((rotated_z + dz) * inverse_length_squared - co.z) * armature_weight,
    }
}
