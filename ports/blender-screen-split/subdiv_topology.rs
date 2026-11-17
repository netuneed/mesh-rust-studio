// SPDX-FileCopyrightText: 2018 Blender Authors
// SPDX-License-Identifier: GPL-2.0-or-later

use std::mem::{MaybeUninit, align_of, size_of};
use std::slice;

type Quad = [i32; 4];

const _: () = assert!(size_of::<Quad>() == 16 && align_of::<Quad>() == 4);

/// Fill the corner tables of one regular patch, excluding its outer face ring.
///
/// # Safety
/// The output pointers must refer to separate writable buffers, each with
/// `quad_count` entries, and must remain exclusive for this call.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn blender_rust_subdiv_inner_loops(
    corner_vertices: *mut Quad,
    corner_edges: *mut Quad,
    quad_count: usize,
    width: i32,
    first_vertex: i32,
    first_edge: i32,
) -> bool {
    if width < 2 || first_vertex < 0 || first_edge < 0 {
        return false;
    }

    let n = i64::from(width);
    if usize::try_from((n - 1) * (n - 1)) != Ok(quad_count)
        || i64::from(first_vertex) + n * n - 1 > i64::from(i32::MAX)
        || i64::from(first_edge) + 2 * n * (n - 1) - 1 > i64::from(i32::MAX)
    {
        return false;
    }

    let Some(vertices_end) = buffer_end(corner_vertices, quad_count) else {
        return false;
    };
    let Some(edges_end) = buffer_end(corner_edges, quad_count) else {
        return false;
    };
    if corner_vertices.addr() < edges_end && corner_edges.addr() < vertices_end {
        return false;
    }

    // Blender gives each coarse face disjoint, validated slices of the destination mesh.
    let vertices = unsafe {
        slice::from_raw_parts_mut(corner_vertices.cast::<MaybeUninit<Quad>>(), quad_count)
    };
    let edges =
        unsafe { slice::from_raw_parts_mut(corner_edges.cast::<MaybeUninit<Quad>>(), quad_count) };
    let row_len = (width - 1) as usize;
    let edge_stride = 2 * width - 1;
    for (y, (vertex_row, edge_row)) in vertices
        .chunks_exact_mut(row_len)
        .zip(edges.chunks_exact_mut(row_len))
        .enumerate()
    {
        let vertex_row_start = first_vertex + y as i32 * width;
        let edge_row_start = first_edge + y as i32 * edge_stride;
        for (x, (vertices, edges)) in vertex_row.iter_mut().zip(edge_row).enumerate() {
            let v = vertex_row_start + x as i32;
            let e = edge_row_start + x as i32;
            vertices.write([v, v + 1, v + width + 1, v + width]);
            edges.write([e, e + width, e + edge_stride, e + width - 1]);
        }
    }
    true
}

/// Write the contiguous face offsets of the subdivided all-quad mesh.
///
/// # Safety
/// `output` must refer to `count` writable and exclusive `i32` entries.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn blender_rust_subdiv_quad_offsets(
    output: *mut i32,
    count: usize,
    first_face: i32,
) -> bool {
    if first_face < 0
        || count == 0
        || count - 1 > (i32::MAX / 4 - first_face).max(0) as usize
        || first_face > i32::MAX / 4
        || buffer_end(output, count).is_none()
    {
        return false;
    }
    let output = unsafe { slice::from_raw_parts_mut(output.cast::<MaybeUninit<i32>>(), count) };
    for (i, offset) in output.iter_mut().enumerate() {
        offset.write(4 * (first_face + i as i32));
    }
    true
}

fn buffer_end<T>(ptr: *mut T, count: usize) -> Option<usize> {
    let len = count.checked_mul(size_of::<T>())?;
    if ptr.is_null() || !ptr.is_aligned() || len > isize::MAX as usize {
        return None;
    }
    ptr.addr().checked_add(len)
}

#[cfg(test)]
mod tests {
    use super::{blender_rust_subdiv_inner_loops, blender_rust_subdiv_quad_offsets};

    #[test]
    fn writes_blenders_face_order_and_winding() {
        for width in [2, 3, 4, 15, 31, 1023] {
            let mut verts = vec![[-1; 4]; ((width - 1) * (width - 1)) as usize];
            let mut edges = verts.clone();
            assert!(unsafe {
                blender_rust_subdiv_inner_loops(
                    verts.as_mut_ptr(),
                    edges.as_mut_ptr(),
                    verts.len(),
                    width,
                    7,
                    13,
                )
            });
            for y in 1..width {
                for x in 1..width {
                    let i = ((y - 1) * (width - 1) + x - 1) as usize;
                    let v0 = 7 + (y - 1) * width + (x - 1);
                    let e0 = 13 + (y - 1) * (2 * width - 1) + (x - 1);
                    assert_eq!(verts[i], [v0, v0 + 1, v0 + width + 1, v0 + width]);
                    assert_eq!(
                        edges[i],
                        [e0, e0 + width, e0 + 2 * width - 1, e0 + width - 1]
                    );
                }
            }
        }
    }

    #[test]
    fn rejects_invalid_and_overlapping_corner_buffers_before_writing() {
        let mut vertices = [[-1; 4]; 4];
        let mut edges = [[-1; 4]; 4];
        let v = vertices.as_mut_ptr();
        let e = edges.as_mut_ptr();
        assert!(!unsafe { blender_rust_subdiv_inner_loops(v, e, 3, 3, 0, 0) });
        assert!(!unsafe { blender_rust_subdiv_inner_loops(v, e, 4, 3, i32::MAX, 0) });
        assert!(!unsafe { blender_rust_subdiv_inner_loops(v, e, 4, 3, 0, i32::MAX) });
        assert!(!unsafe { blender_rust_subdiv_inner_loops(v, v, 4, 3, 0, 0) });
        assert!(!unsafe {
            blender_rust_subdiv_inner_loops(v, e.cast::<u8>().wrapping_add(1).cast(), 4, 3, 0, 0)
        });
        assert!(!unsafe { blender_rust_subdiv_inner_loops(v, std::ptr::null_mut(), 4, 3, 0, 0) });
        assert_eq!(vertices, [[-1; 4]; 4]);
        assert_eq!(edges, [[-1; 4]; 4]);
    }

    #[test]
    fn writes_face_offsets_and_checks_limits() {
        let mut offsets = [-1; 3];
        let ptr = offsets.as_mut_ptr();
        assert!(!unsafe { blender_rust_subdiv_quad_offsets(ptr, 3, -1) });
        assert!(!unsafe { blender_rust_subdiv_quad_offsets(ptr, 3, i32::MAX / 4 - 1) });
        assert_eq!(offsets, [-1; 3]);
        assert!(unsafe { blender_rust_subdiv_quad_offsets(ptr, 3, 27) });
        assert_eq!(offsets, [108, 112, 116]);
    }
}
