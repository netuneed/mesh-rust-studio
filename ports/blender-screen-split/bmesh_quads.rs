/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

use core::ffi::c_void;
use core::mem::{align_of, offset_of, size_of};
use core::ptr::{addr_of, addr_of_mut, null_mut};

#[repr(C)]
struct QuadInput {
    verts: [*mut c_void; 4],
    edges: [*mut MeshEdge; 4],
}

#[repr(C)]
struct MeshEdge {
    prefix: [u8; 32],
    radial_head: *mut MeshLoop,
    suffix: [u8; 32],
}

#[repr(C)]
struct MeshHeader {
    data: *mut c_void,
    index: i32,
    element_type: u8,
    flags: u8,
    api_flags: u8,
}

#[repr(C)]
struct MeshLoop {
    head: MeshHeader,
    vert: *mut c_void,
    edge: *mut MeshEdge,
    face: *mut c_void,
    radial_next: *mut MeshLoop,
    radial_prev: *mut MeshLoop,
    next: *mut MeshLoop,
    prev: *mut MeshLoop,
}

const _: () = {
    assert!(size_of::<QuadInput>() == 64 && align_of::<QuadInput>() == 8);
    assert!(size_of::<MeshEdge>() == 72 && offset_of!(MeshEdge, radial_head) == 32);
    assert!(size_of::<MeshHeader>() == 16 && offset_of!(MeshHeader, element_type) == 12);
    assert!(size_of::<MeshLoop>() == 72 && align_of::<MeshLoop>() == 8);
    assert!(offset_of!(MeshLoop, vert) == 16 && offset_of!(MeshLoop, face) == 32);
    assert!(offset_of!(MeshLoop, radial_next) == 40 && offset_of!(MeshLoop, prev) == 64);
};

/// # Safety
/// C++ owns the initial input table, face allocation table and loop allocation
/// table, with `count`, `count`, and `count * 4` entries respectively. C++
/// validates the quad's input indices and endpoints and allocates distinct,
/// aligned, writable loop objects. The input's edges have valid radial cycles
/// (or null heads), and this call holds exclusive access to all touched
/// topology. Face and vertex pointers are stored only.
#[unsafe(no_mangle)]
unsafe extern "C" fn blender_rust_bmesh_quad_batch(
    input: *const QuadInput,
    faces: *const *mut c_void,
    loops: *const *mut MeshLoop,
    count: u32,
    first_loop_index: i32,
) -> u8 {
    let count = count as usize;
    if !(1..=128).contains(&count)
        || first_loop_index < 0
        || first_loop_index.checked_add((count * 4 - 1) as i32).is_none()
        || input.is_null()
        || faces.is_null()
        || loops.is_null()
        || !input.is_aligned()
        || !faces.is_aligned()
        || !loops.is_aligned()
    {
        return 0;
    }

    // SAFETY: C++ allocates and validates the disjoint loop output slots and
    // supplies a fresh BMesh with exclusive access and valid radial links.
    unsafe {
        for i in 0..count {
            let quad = &*input.add(i);
            let face = faces.add(i).read();
            let corners = loops.add(i * 4).cast::<[*mut MeshLoop; 4]>().read();
            for j in 0..4 {
                let node = corners[j];
                let edge = quad.edges[j];
                let radial_head = addr_of_mut!((*edge).radial_head);
                let previous = radial_head.read();
                let (radial_next, radial_prev) = if previous.is_null() {
                    (node, node)
                } else {
                    let next = addr_of!((*previous).radial_next).read();
                    addr_of_mut!((*next).radial_prev).write(node);
                    addr_of_mut!((*previous).radial_next).write(node);
                    (next, previous)
                };
                node.write(MeshLoop {
                    head: MeshHeader {
                        data: null_mut(),
                        index: first_loop_index + (i * 4 + j) as i32,
                        element_type: 4,
                        flags: 0,
                        api_flags: 0,
                    },
                    vert: quad.verts[j],
                    edge,
                    face,
                    radial_next,
                    radial_prev,
                    next: corners[(j + 1) % 4],
                    prev: corners[(j + 3) % 4],
                });
                radial_head.write(node);
            }
        }
    }
    1
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::mem::MaybeUninit;

    #[test]
    fn preserves_face_winding_and_non_manifold_radial_insertion_order() {
        let mut edge_storage: [MeshEdge; 4] = core::array::from_fn(|_| MeshEdge {
            prefix: [0x63; 32],
            radial_head: null_mut(),
            suffix: [0x78; 32],
        });
        let edges = core::array::from_fn(|j| &raw mut edge_storage[j]);
        // Three coincident faces share all four edges, including the middle
        // face with winding opposite to its neighbors.
        let verts = [1, 2, 3, 4].map(core::ptr::without_provenance_mut);
        let quads = [
            QuadInput { verts, edges },
            QuadInput {
                verts: [verts[0], verts[3], verts[2], verts[1]],
                edges: [edges[3], edges[2], edges[1], edges[0]],
            },
            QuadInput { verts, edges },
        ];
        let faces = [5, 6, 7].map(core::ptr::without_provenance_mut);
        let mut loop_storage: [MaybeUninit<MeshLoop>; 12] =
            [const { MaybeUninit::uninit() }; 12];
        let loops: [_; 12] = core::array::from_fn(|i| loop_storage[i].as_mut_ptr());
        assert_eq!(
            unsafe {
                blender_rust_bmesh_quad_batch(
                    quads.as_ptr(),
                    faces.as_ptr(),
                    loops.as_ptr(),
                    3,
                    11,
                )
            },
            1
        );
        for i in 0..3 {
            for j in 0..4 {
                let node = unsafe { &*loops[4 * i + j] };
                assert_eq!(node.head.index, (11 + 4 * i + j) as i32);
                assert_eq!(node.head.element_type, 4);
                assert_eq!(node.head.flags, 0);
                assert_eq!(node.head.api_flags, 0);
                assert!(node.head.data.is_null());
                assert_eq!(node.vert, quads[i].verts[j]);
                assert_eq!(node.edge, quads[i].edges[j]);
                assert_eq!(node.face, faces[i]);
                assert_eq!(node.next, loops[4 * i + (j + 1) % 4]);
                assert_eq!(node.prev, loops[4 * i + (j + 3) % 4]);
            }
        }
        for (j, edge) in edge_storage.iter().enumerate() {
            let a = loops[j];
            let b = loops[7 - j];
            let c = loops[8 + j];
            assert_eq!(edge.radial_head, c);
            for (node, next, previous) in [(a, b, c), (b, c, a), (c, a, b)] {
                assert_eq!(unsafe { (*node).radial_next }, next);
                assert_eq!(unsafe { (*node).radial_prev }, previous);
            }
            assert_eq!(edge.prefix, [0x63; 32]);
            assert_eq!(edge.suffix, [0x78; 32]);
        }
    }

    #[test]
    fn rejects_oversized_null_or_overflowing_batches() {
        let quad = QuadInput {
            verts: [null_mut(); 4],
            edges: [null_mut(); 4],
        };
        let face = null_mut();
        let node = null_mut();
        for (ptr, count, index) in [
            (&raw const quad, 0, 0),
            (&raw const quad, 129, 0),
            (&raw const quad, 1, i32::MAX - 2),
            (&raw const quad, 1, -1),
            (core::ptr::null(), 1, 0),
        ] {
            assert_eq!(
                unsafe { blender_rust_bmesh_quad_batch(ptr, &face, &node, count, index) },
                0
            );
        }
    }
}
