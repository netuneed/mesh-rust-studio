/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

use core::mem::{align_of, size_of};
use core::ptr::{addr_of_mut, null_mut};

#[repr(C)]
struct FreeNode {
    next: *mut FreeNode,
    freeword: isize,
}

unsafe fn initialize_links<const ITER: bool>(
    data: *mut u8,
    count: usize,
    stride: usize,
    freeword: isize,
) {
    // SAFETY: The caller owns the full checked chunk. Each offset belongs to a
    // distinct, aligned slot. No part of an uninitialized node is read.
    unsafe {
        let mut node = data;
        for _ in 1..count {
            let next = node.add(stride);
            let entry = node.cast::<FreeNode>();
            addr_of_mut!((*entry).next).write(next.cast());
            if ITER {
                addr_of_mut!((*entry).freeword).write(freeword);
            }
            node = next;
        }
        let last = node.cast::<FreeNode>();
        addr_of_mut!((*last).next).write(null_mut());
        if ITER {
            addr_of_mut!((*last).freeword).write(freeword);
        }
    }
}

/// # Safety
/// C++ owns the allocation and guarantees exclusive writable access to a
/// detached data region of `count * stride` bytes. The first slot begins at
/// `data`. No element is initialized or published until this call completes.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn blender_rust_bmesh_pool_chunk(
    data: *mut u8,
    count: u32,
    stride: u32,
    iter_flag: u32,
    freeword: isize,
) -> u8 {
    let count = count as usize;
    let stride = stride as usize;
    let Some(bytes) = count.checked_mul(stride) else {
        return 0;
    };
    if data.is_null()
        || !data.cast::<FreeNode>().is_aligned()
        || count == 0
        || stride < size_of::<FreeNode>()
        || !stride.is_multiple_of(align_of::<FreeNode>())
        || bytes > isize::MAX as usize
        || data.addr().checked_add(bytes).is_none()
        || iter_flag > 1
    {
        return 0;
    }
    // SAFETY: The caller supplies the storage. The checks above bound every
    // slot access. Both paths write the same next pointers used by BLI.
    unsafe {
        if iter_flag == 1 {
            initialize_links::<true>(data, count, stride, freeword);
        } else {
            initialize_links::<false>(data, count, stride, freeword);
        }
    }
    1
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::ptr::without_provenance_mut;

    #[repr(C)]
    struct Slot {
        node: FreeNode,
        payload: [usize; 3],
    }

    #[test]
    fn preserves_unwritten_bytes_and_links_in_allocation_order() {
        let freeword = 0x6572;
        for iter_flag in [0, 1] {
            for count in [1, 7] {
                let mut slots: Vec<_> = (0..count)
                    .map(|_| Slot {
                        node: FreeNode {
                            next: null_mut(),
                            freeword: -1,
                        },
                        payload: [0x85; 3],
                    })
                    .collect();
                let ptr = slots.as_mut_ptr();
                let stride = size_of::<Slot>() as u32;
                assert_eq!(
                    unsafe {
                        blender_rust_bmesh_pool_chunk(
                            ptr.cast(),
                            count,
                            stride,
                            iter_flag,
                            freeword,
                        )
                    },
                    1
                );
                for (i, slot) in slots.iter().enumerate() {
                    let expected_next = if i + 1 == slots.len() {
                        null_mut()
                    } else {
                        ptr.wrapping_add(i + 1).cast()
                    };
                    assert_eq!(slot.node.next, expected_next);
                    assert_eq!(slot.node.freeword, if iter_flag == 1 { freeword } else { -1 });
                    assert_eq!(slot.payload, [0x85; 3]);
                }
            }
        }
    }

    #[test]
    fn invalid_input_leaves_storage_untouched() {
        let mut data = [0x3dusize; 16];
        let ptr = data.as_mut_ptr().cast::<u8>();
        let stride = size_of::<FreeNode>() as u32;
        for (start, count, stride, flag) in [
            (null_mut(), 3, stride, 1),
            (ptr, 0, stride, 1),
            (ptr, 3, stride - 1, 1),
            (ptr, 3, stride + 1, 1),
            (ptr.wrapping_add(1), 3, stride, 1),
            (ptr, 3, stride, 2),
            (ptr, u32::MAX, u32::MAX - 7, 1),
            (without_provenance_mut(usize::MAX - 15), 3, stride, 1),
        ] {
            assert_eq!(
                unsafe { blender_rust_bmesh_pool_chunk(start, count, stride, flag, 0) },
                0
            );
            assert_eq!(data, [0x3d; 16]);
        }
    }
}