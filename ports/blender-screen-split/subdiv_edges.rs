// SPDX-FileCopyrightText: 2018 Blender Authors
// SPDX-License-Identifier: GPL-2.0-or-later

use std::mem::{align_of, size_of};
use std::slice;

type Edge = [i32; 2];

/// Writes the interior edges of one regular subdivision patch in Blender order.
///
/// # Safety
/// A non-null output must refer to `count` writable, exclusive pairs of i32.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn blender_rust_subdiv_inner_edges(
    output: *mut Edge,
    count: usize,
    width: i32,
    first_vertex: i32,
) -> bool {
    if width < 2
        || first_vertex < 0
        || output.is_null()
        || !output.is_aligned()
        || count > isize::MAX as usize / size_of::<Edge>()
    {
        return false;
    }

    let width = i64::from(width);
    let first_vertex = i64::from(first_vertex);
    let expected = 2 * width * (width - 1);
    if usize::try_from(expected) != Ok(count)
        || first_vertex + width * width - 1 > i64::from(i32::MAX)
    {
        return false;
    }

    // The C++ caller passes a disjoint slice of the allocated mesh edge table.
    let output =
        unsafe { slice::from_raw_parts_mut(output.cast::<std::mem::MaybeUninit<Edge>>(), count) };
    let mut edges = output.iter_mut();
    let width = width as i32;
    let first = first_vertex as i32;
    let write_row =
        |edges: &mut slice::IterMut<'_, std::mem::MaybeUninit<Edge>>, start, stride, len| {
            for (x, output_edge) in (0..len).zip(edges) {
                let vertex = start + x;
                output_edge.write([vertex, vertex + stride]);
            }
        };

    write_row(&mut edges, first, 1, width - 1);
    for y in 0..width - 1 {
        let start = first + y * width;
        write_row(&mut edges, start, width, width);
        write_row(&mut edges, start + width, 1, width - 1);
    }
    true
}

const _: () = assert!(size_of::<Edge>() == 8 && align_of::<Edge>() == 4);

#[cfg(test)]
mod tests {
    use super::blender_rust_subdiv_inner_edges;

    #[test]
    fn emits_blenders_horizontal_then_vertical_rows() {
        let mut edges = [[-1, -1]; 12];
        assert!(unsafe { blender_rust_subdiv_inner_edges(edges.as_mut_ptr(), edges.len(), 3, 10) });
        assert_eq!(
            edges,
            [
                [10, 11],
                [11, 12],
                [10, 13],
                [11, 14],
                [12, 15],
                [13, 14],
                [14, 15],
                [13, 16],
                [14, 17],
                [15, 18],
                [16, 17],
                [17, 18],
            ]
        );
    }

    #[test]
    fn rejects_invalid_arguments_before_writing() {
        let mut edges = [[-1, -1]; 4];
        let output = edges.as_mut_ptr();
        assert!(!unsafe { blender_rust_subdiv_inner_edges(output, 3, 2, 0) });
        assert!(!unsafe { blender_rust_subdiv_inner_edges(output, 4, 2, -1) });
        assert!(!unsafe { blender_rust_subdiv_inner_edges(output, 4, 2, i32::MAX) });
        assert!(!unsafe { blender_rust_subdiv_inner_edges(std::ptr::null_mut(), 4, 2, 0) });
        assert_eq!(edges, [[-1, -1]; 4]);
    }
}
