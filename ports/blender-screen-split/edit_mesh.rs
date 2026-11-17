// SPDX-FileCopyrightText: 2001-2002 NaN Holding BV. All rights reserved.
// SPDX-License-Identifier: GPL-2.0-or-later

#[repr(C)]
pub struct EditMeshVector {
    values: [f32; 3],
}

#[repr(C)]
pub struct EditMeshInverse {
    columns: [[f32; 3]; 3],
}

const _: () = {
    use std::mem::{align_of, offset_of, size_of};

    assert!(size_of::<EditMeshVector>() == 12 && align_of::<EditMeshVector>() == 4);
    assert!(offset_of!(EditMeshVector, values) == 0);
    assert!(size_of::<EditMeshInverse>() == 36 && align_of::<EditMeshInverse>() == 4);
    assert!(offset_of!(EditMeshInverse, columns) == 0);
};

/// Translate an ordinary mesh vertex in Blender 5.3.0 Edit Mode.
/// The operator supplies its constrained displacement and the vertex's transform to local space.
#[unsafe(no_mangle)]
pub extern "C" fn blender_edit_mesh_translate_vertex(
    original: EditMeshVector,
    displacement: EditMeshVector,
    inverse: EditMeshInverse,
    factor: f32,
    trace: u8,
) -> EditMeshVector {
    let [x, y, z] = displacement.values;
    let [first, second, third] = inverse.columns;
    // Keep the separate floating-point operations and evaluation order of Blender's
    // mul_m3_v3, mul_v3_fl, then add_v3_v3v3.
    let local_delta: [f32; 3] =
        std::array::from_fn(|axis| (first[axis] * x + second[axis] * y + third[axis] * z) * factor);
    let values = std::array::from_fn(|axis| original.values[axis] + local_delta[axis]);

    if trace == 1 && trace_enabled() {
        use std::io::Write;
        let _ = writeln!(
            std::io::stderr().lock(),
            "RUST_EDIT_MESH_TRANSLATE input={:?} delta={local_delta:?} output={values:?}",
            original.values,
        );
    }

    EditMeshVector { values }
}

fn trace_enabled() -> bool {
    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ENABLED.get_or_init(|| {
        std::env::var_os("BLENDER_RUST_EDIT_MESH_TRACE").as_deref()
            == Some(std::ffi::OsStr::new("1"))
    })
}
