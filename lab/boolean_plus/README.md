# Boolean Plus

Select the mesh you want to change. In Properties, open **Modifiers > Add Modifier > Blender Plus > Boolean Plus**. Pick a second mesh with the **Cutter** eyedropper. If two meshes are selected when you add the modifier, the active mesh receives the modifier and the other mesh becomes its cutter.

| Operation | Result |
| --- | --- |
| Difference | Removes the part of the target inside the cutter. |
| Union | Combines the target and cutter into one volume. |
| Intersection | Keeps the volume shared by both meshes. |

Choose **Auto** for interactive work. It can use the Manifold solver for suitable closed meshes and falls back to Exact for open geometry or to verify an empty result. Choose **Exact** to use the exact solver when the bounding boxes overlap. **Cutter Display** changes how the cutter appears in the viewport. Wire display makes it easier to see a cut.

### Preview and apply

Open **Tools** on the modifier you want to edit. You can select or move its cutter and adjust supported Array counts. **Preview Level**, **Use Preview for G** and **GPU Cut Preview** are shared across the current session. Each modifier keeps its own cutter, operation and solver.

To move selected cutter vertices for a supported Difference, enter Edit Mode on the cutter, select vertices and search for **Move Cutter Vertices with Preview** with F3. You can also pin the target in Properties before entering Edit Mode and use the tool on its modifier card. Press X, Y or Z to constrain movement, Enter or left-click to confirm, or Esc to cancel. The full modifier stack recalculates on confirmation. Closed nonconvex cutter shells can preview on the GPU. Supported fallback cases use a simplified mesh calculated by a background Blender process.

**Apply Boolean Plus** applies this modifier when it is first in the stack. When other modifiers precede it, **Bake Visible Stack** converts the whole visible stack to mesh after confirmation. This option is available in Object Mode for meshes without shape keys.

Saved modifiers retain their inputs and their position in the stack. Early versions with fixed inputs show **Enable Editable Controls**. Use it to expose the editable inputs while keeping the existing cutter.
