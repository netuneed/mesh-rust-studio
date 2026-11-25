// SPDX-FileCopyrightText: 2026 Blender+ RUST contributors
// SPDX-License-Identifier: GPL-2.0-or-later

use std::slice::{from_raw_parts, from_raw_parts_mut};

fn group_triangles(face_ids: &[u32], offsets: &mut [i32], triangles: &mut [i32]) -> bool {
    let Some(face_count) = offsets.len().checked_sub(1) else {
        return false;
    };
    if triangles.len() != face_ids.len() || i32::try_from(face_ids.len()).is_err() {
        return false;
    }

    offsets.fill(0);
    for &face in face_ids {
        let group = face as usize;
        if group >= face_count {
            return false;
        }
        offsets[group + 1] += 1;
    }

    let mut end = 0;
    for count in &mut offsets[1..] {
        end += *count;
        *count = end;
    }

    // Reverse scatter reuses the end offsets as write cursors and keeps each face's
    // triangles in their original order for Blender's polygon assembly.
    for (index, &face) in face_ids.iter().enumerate().rev() {
        let cursor = &mut offsets[face as usize + 1];
        *cursor -= 1;
        triangles[*cursor as usize] = index as i32;
    }
    offsets.copy_within(1.., 0);
    offsets[face_count] = end;
    true
}

/// Writes a compact, stable grouping of Boolean output triangles by original face ID.
///
/// Returns zero for invalid input. The caller must discard the output in that case.
///
/// # Safety
///
/// The caller must provide aligned, initialized input for triangle_count face IDs
/// and writable, initialized arrays of face_count + 1 offsets and triangle_count
/// triangle indices. All arrays must be valid and disjoint for the duration of the call.
/// The face ID and triangle pointers may be null when triangle_count is zero.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn blender_rust_boolean_group_triangles(
    face_ids: *const u32,
    triangle_count: usize,
    offsets: *mut i32,
    face_count: usize,
    triangles: *mut i32,
) -> u8 {
    let Some(offset_count) = face_count.checked_add(1) else {
        return 0;
    };
    let max_elements = (isize::MAX as usize) / size_of::<i32>();
    if offset_count > max_elements
        || triangle_count > max_elements
        || triangle_count > i32::MAX as usize
        || offsets.is_null()
        || !offsets.is_aligned()
        || (triangle_count != 0
            && (face_ids.is_null()
                || !face_ids.is_aligned()
                || triangles.is_null()
                || !triangles.is_aligned()))
    {
        return 0;
    }

    // SAFETY: the caller guarantees storage and disjointness. Empty arrays need no pointer.
    let offsets = unsafe { from_raw_parts_mut(offsets, offset_count) };
    let (face_ids, triangles) = if triangle_count == 0 {
        (&[][..], &mut [][..])
    } else {
        unsafe {
            (
                from_raw_parts(face_ids, triangle_count),
                from_raw_parts_mut(triangles, triangle_count),
            )
        }
    };
    u8::from(group_triangles(face_ids, offsets, triangles))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_input_order_inside_each_face_including_empty_groups() {
        let mut offsets = [0; 6];
        let mut triangles = [0; 7];
        assert!(group_triangles(
            &[2, 0, 2, 4, 0, 2, 4],
            &mut offsets,
            &mut triangles
        ));
        assert_eq!(offsets, [0, 2, 2, 5, 5, 7]);
        assert_eq!(triangles, [1, 4, 0, 2, 5, 3, 6]);
    }

    #[test]
    fn accepts_empty_meshes_without_dereferencing_null() {
        let mut offsets = [-4; 4];
        let result = unsafe {
            blender_rust_boolean_group_triangles(
                std::ptr::null(),
                0,
                offsets.as_mut_ptr(),
                3,
                std::ptr::null_mut(),
            )
        };
        assert_eq!((result, offsets), (1, [0; 4]));
    }

    #[test]
    fn rejects_ids_outside_the_source_faces() {
        assert!(!group_triangles(&[2], &mut [0; 3], &mut [0]));
    }
}
