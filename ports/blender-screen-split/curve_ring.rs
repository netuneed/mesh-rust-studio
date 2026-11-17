// SPDX-FileCopyrightText: 2023 Blender Authors
// SPDX-License-Identifier: GPL-2.0-or-later

const PROFILE_SIZE: usize = 10;

#[repr(C)]
pub struct CurveRingMatrix {
    columns: [[f32; 3]; 4],
}

#[repr(C)]
pub struct CurveRingPoints {
    points: [[f32; 3]; PROFILE_SIZE],
}

const _: () = {
    use std::mem::{align_of, offset_of, size_of};

    assert!(size_of::<CurveRingMatrix>() == 48 && align_of::<CurveRingMatrix>() == 4);
    assert!(size_of::<CurveRingPoints>() == 120 && align_of::<CurveRingPoints>() == 4);
    assert!(offset_of!(CurveRingMatrix, columns) == 0);
    assert!(offset_of!(CurveRingPoints, points) == 0);
};

/// Apply the ready Curve to Mesh frame to one ten-point profile ring.
#[unsafe(no_mangle)]
pub extern "C" fn blender_rust_curve_ring(
    matrix: CurveRingMatrix,
    profile: CurveRingPoints,
    trace_rings: u32,
) -> CurveRingPoints {
    let [u, v, w, location] = matrix.columns;
    let points = profile.points.map(|[x, y, z]| {
        std::array::from_fn(|axis| {
            // BLI MatView starts the column accumulation with +0.0, then adds location.
            (((0.0 + x * u[axis]) + y * v[axis]) + z * w[axis]) + location[axis]
        })
    });

    if trace_rings != 0 && trace_enabled() {
        use std::io::Write;
        let line = format!("RUST_CURVE_RING rings={trace_rings} points={PROFILE_SIZE}\n");
        let _ = std::io::stderr().lock().write_all(line.as_bytes());
    }
    CurveRingPoints { points }
}

fn trace_enabled() -> bool {
    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ENABLED.get_or_init(|| {
        std::env::var_os("BLENDER_RUST_CURVE_RING_TRACE").as_deref()
            == Some(std::ffi::OsStr::new("1"))
    })
}
