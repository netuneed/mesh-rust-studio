"""Draw an interactive Difference from the worker's prepared triangle inputs."""

import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np

from mathutils import Matrix, Vector


_VERTEX = """
void main()
{
    worldPosition = position;
    operandIsland = int(islandIndex);
    if (surfaceKind == 1) {
        worldPosition += cutterDelta;
    }
    gl_Position = viewProjection * vec4(worldPosition, 1.0);
}
"""


_FRAGMENT = """
#define gridOrigin cutSettings.originAndEpsilon.xyz
#define positionEpsilon cutSettings.originAndEpsilon.w
#define gridInverseStep cutSettings.inverseAndSide.xyz
#define gridSide int(cutSettings.inverseAndSide.w)

vec4 readElement(sampler2D image, int index)
{
    return texelFetch(image, ivec2(index % 256, index / 256), 0);
}

bool outsideBounds(vec3 point, vec3 lo, vec3 hi)
{
    return any(lessThan(point, lo)) || any(greaterThan(point, hi));
}

#ifdef BOOLEAN_NONCONVEX
bool insideNonconvex(vec3 point, int island, int root, float margin);
#endif

bool insideCutter(vec3 point, int excludedIsland, float margin)
{
    ivec3 cell = ivec3(floor((point - gridOrigin) * gridInverseStep));
    if (any(lessThan(cell, ivec3(0))) || any(greaterThanEqual(cell, ivec3(gridSide)))) {
        return false;
    }
    vec4 info = readElement(gridTable, (cell.z * gridSide + cell.y) * gridSide + cell.x);
    for (int i = 0; i < int(info.y); i++) {
        int island = int(readElement(gridTable, int(info.x) + i).x);
        if (island == excludedIsland) {
            continue;
        }
        int index = island * 2;
        vec4 low = readElement(cutterBounds, index);
        vec4 high = readElement(cutterBounds, index + 1);
        if (outsideBounds(point, low.xyz, high.xyz)) {
            continue;
        }
#ifdef BOOLEAN_NONCONVEX
        if (high.w < 0.0) {
            if (insideNonconvex(point, island, int(low.w), margin)) {
                return true;
            }
            continue;
        }
#endif
        bool inside = true;
        for (int pl = 0; pl < int(high.w); pl++) {
            vec4 plane = readElement(cutterPlanes, int(low.w) + pl);
            if (dot(plane.xyz, point) - plane.w > margin) {
                inside = false;
                break;
            }
        }
        if (inside) {
            return true;
        }
    }
    return false;
}

bool rayOverlaps(vec3 origin, vec3 inverse, vec3 lo, vec3 hi, float minDistance)
{
    vec3 begin = (lo - origin) * inverse;
    vec3 end = (hi - origin) * inverse;
    vec3 nearest = min(begin, end);
    vec3 farthest = max(begin, end);
    float a = max(max(nearest.x, nearest.y), nearest.z);
    float b = min(min(farthest.x, farthest.y), farthest.z);
    return b >= max(minDistance, a);
}

int rayTriangle(vec3 p, vec3 ray, int i)
{
    vec3 a = readElement(targetTriangles, i * 3).xyz;
    vec3 b = readElement(targetTriangles, i * 3 + 1).xyz;
    vec3 c = readElement(targetTriangles, i * 3 + 2).xyz;
    vec3 e1 = b - a;
    vec3 e2 = c - a;
    vec3 h = cross(ray, e2);
    float det = dot(e1, h);
    if (abs(det) < 1.0e-10) {
        return 0;
    }
    vec3 s = p - a;
    float u = dot(s, h) / det;
    if (u < -1.0e-6 || u > 1.000001) {
        return 0;
    }
    vec3 q = cross(s, e1);
    float v = dot(ray, q) / det;
    float w = 1.0 - u - v;
    if (v < -1.0e-6 || w < -1.0e-6 || dot(e2, q) / det <= positionEpsilon) {
        return 0;
    }
    return min(min(u, v), w) < 1.0e-6 ? -1 : 1;
}

int targetParity(vec3 point, vec3 ray)
{
    vec3 inverse = 1.0 / ray;
    int stack[32];
    stack[0] = 0;
    int size = 1;
    bool inside = false;
    while (size > 0) {
        int nodeIndex = stack[--size] * 2;
        vec4 low = readElement(targetBvh, nodeIndex);
        vec4 high = readElement(targetBvh, nodeIndex + 1);
        if (!rayOverlaps(point, inverse, low.xyz, high.xyz, positionEpsilon)) {
            continue;
        }
        if (high.w < 0.0) {
            stack[size++] = int(-high.w);
            stack[size++] = int(low.w);
        }
        else {
            for (int i = 0; i < int(high.w); i++) {
                int hit = rayTriangle(point, ray, int(low.w) + i);
                if (hit < 0) {
                    return -1;
                }
                if (hit > 0) {
                    inside = !inside;
                }
            }
        }
    }
    return inside ? 1 : 0;
}

bool insideTarget(vec3 point)
{
    int parity = targetParity(point, normalize(vec3(0.931, 0.309, 0.196)));
    if (parity < 0) {
        parity = targetParity(point, normalize(vec3(0.781, -0.497, 0.379)));
    }
    return parity > 0;
}

void main()
{
    if (surfaceKind == 0 && insideCutter(worldPosition - cutterDelta, -1, positionEpsilon)) {
        discard;
    }
    if (surfaceKind == 1 && (!insideTarget(worldPosition) ||
        insideCutter(worldPosition - cutterDelta, operandIsland, -positionEpsilon))) {
        discard;
    }
    if (diagnosticMode != 0) {
        fragColor = surfaceKind == 1 ? vec4(0.9, 0.2, 0.1, 1.0) : vec4(0.45, 0.65, 0.85, 1.0);
        return;
    }
    fragColor = shadeSurface(worldPosition);
}
"""


_NONCONVEX = """
float segmentDistanceSquared(vec3 point, vec3 a, vec3 b)
{
    vec3 edge = b - a;
    float t = clamp(dot(point - a, edge) / dot(edge, edge), 0.0, 1.0);
    vec3 delta = point - a - t * edge;
    return dot(delta, delta);
}

bool nearTriangle(vec3 point, int index, float radiusSquared)
{
    vec3 a = readElement(cutterRayTriangles, index * 3).xyz;
    vec3 b = readElement(cutterRayTriangles, index * 3 + 1).xyz;
    vec3 c = readElement(cutterRayTriangles, index * 3 + 2).xyz;
    vec3 normal = cross(b - a, c - a);
    float planeDistance = dot(point - a, normal);
    if (planeDistance * planeDistance > radiusSquared * dot(normal, normal)) {
        return false;
    }
    // The cross products have the same orientation for the point and its
    // projection onto the triangle plane when dotted with the normal.
    if (dot(cross(b - a, point - a), normal) >= 0.0 &&
        dot(cross(c - b, point - b), normal) >= 0.0 &&
        dot(cross(a - c, point - c), normal) >= 0.0) {
        return true;
    }
    return (segmentDistanceSquared(point, a, b) <= radiusSquared ||
            segmentDistanceSquared(point, b, c) <= radiusSquared ||
            segmentDistanceSquared(point, c, a) <= radiusSquared);
}

bool nearCutterSurface(vec3 point, int root, float radius)
{
    int stack[32];
    stack[0] = root;
    int size = 1;
    float radiusSquared = radius * radius;
    while (size > 0) {
        int node = stack[--size] * 2;
        vec4 low = readElement(cutterBvh, node);
        vec4 high = readElement(cutterBvh, node + 1);
        if (outsideBounds(point, low.xyz - radius, high.xyz + radius)) {
            continue;
        }
        if (high.w < 0.0) {
            stack[size++] = int(-high.w);
            stack[size++] = int(low.w);
        }
        else {
            for (int i = 0; i < int(high.w); i++) {
                if (nearTriangle(point, int(low.w) + i, radiusSquared)) {
                    return true;
                }
            }
        }
    }
    return false;
}

int cutterRayTriangle(vec3 point, vec3 ray, int index)
{
    vec3 a = readElement(cutterRayTriangles, index * 3).xyz;
    vec3 b = readElement(cutterRayTriangles, index * 3 + 1).xyz;
    vec3 c = readElement(cutterRayTriangles, index * 3 + 2).xyz;
    vec3 e1 = b - a;
    vec3 e2 = c - a;
    vec3 normal = cross(e1, e2);
    vec3 h = cross(ray, e2);
    float determinant = dot(e1, h);
    if (determinant * determinant <= 1.0e-14 * dot(normal, normal)) {
        return 0;
    }
    vec3 s = point - a;
    float u = dot(s, h) / determinant;
    const float barycentricTolerance = 2.0e-6;
    if (u < -barycentricTolerance || u > 1.0 + barycentricTolerance) {
        return 0;
    }
    vec3 q = cross(s, e1);
    float v = dot(ray, q) / determinant;
    float w = 1.0 - u - v;
    float crossing = dot(e2, q) / determinant;
    if (v < -barycentricTolerance || w < -barycentricTolerance || crossing <= 0.0) {
        return 0;
    }
    return min(min(u, v), w) <= barycentricTolerance ? -1 : 1;
}

int cutterParity(vec3 point, vec3 ray, int root)
{
    vec3 inverse = 1.0 / ray;
    int stack[32];
    stack[0] = root;
    int size = 1;
    bool inside = false;
    while (size > 0) {
        int node = stack[--size] * 2;
        vec4 low = readElement(cutterBvh, node);
        vec4 high = readElement(cutterBvh, node + 1);
        if (!rayOverlaps(point, inverse, low.xyz, high.xyz, 0.0)) {
            continue;
        }
        if (high.w < 0.0) {
            stack[size++] = int(-high.w);
            stack[size++] = int(low.w);
        }
        else {
            for (int i = 0; i < int(high.w); i++) {
                int hit = cutterRayTriangle(point, ray, int(low.w) + i);
                if (hit < 0) {
                    return -1;
                }
                if (hit > 0) {
                    inside = !inside;
                }
            }
        }
    }
    return inside ? 1 : 0;
}

bool insideNonconvex(vec3 point, int island, int root, float margin)
{
    point -= readElement(cutterOffsets, island).xyz;
    if (nearCutterSurface(point, root, abs(margin))) {
        return margin > 0.0;
    }
    int parity = cutterParity(point, normalize(vec3(0.931, 0.309, 0.196)), root);
    if (parity < 0) {
        parity = cutterParity(point, normalize(vec3(0.781, -0.497, 0.379)), root);
    }
    if (parity < 0) {
        parity = cutterParity(point, normalize(vec3(-0.271, 0.527, 0.806)), root);
    }
    return parity > 0;
}
"""


_SHADE = """
vec4 shadeSurface(vec3 point)
{
    vec3 normal = normalize(cross(dFdx(point), dFdy(point)));
    float light = 0.25 + 0.75 * abs(dot(normal, normalize(vec3(0.37, -0.22, 0.9))));
    return vec4(vec3(0.63, 0.68, 0.74) * light, 1.0);
}
"""


def _draw_in_view(draw, projection):
    depth_test, depth_mask = gpu.state.depth_test_get(), gpu.state.depth_mask_get()
    try:
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.depth_mask_set(True)
        draw(projection)
    finally:
        gpu.state.depth_mask_set(depth_mask)
        gpu.state.depth_test_set(depth_test)


def _texture(table):
    stride = 256
    height = (len(table) + stride - 1) // stride
    data = np.zeros((height * stride, 4), dtype="f4")
    data[:len(table)] = table
    return gpu.types.GPUTexture((stride, height), format="RGBA32F",
                                data=gpu.types.Buffer("FLOAT", data.size, data.reshape(-1)))


class SurfaceDisplay:
    """Immutable target resources shared by cutter-only GPU refreshes."""

    def __init__(self, tables, shader):
        self.bvh = _texture(tables["target_bvh"])
        self.triangles = _texture(tables["target_bvh_triangles"])
        positions = tables["target_positions"]
        membership = np.full(len(positions), -1.0, dtype="f4")
        self.batch = batch_for_shader(shader, "TRIS", {"position": positions, "islandIndex": membership},
                                      indices=tables["target_triangles"].tolist())
        self.world_matrix = Matrix(tables["target_matrix_world"].tolist())
        self.inverse_linear = self.world_matrix.inverted().to_3x3()


class DifferenceDisplay:
    _shaders = {}
    _cutter_textures = {"gridTable": "cutter_grid", "cutterBounds": "cutter_bounds",
                        "cutterPlanes": "cutter_planes"}
    _nonconvex_textures = {"cutterBvh": "cutter_bvh", "cutterRayTriangles": "cutter_ray_triangles",
                           "cutterOffsets": "cutter_offsets"}

    @classmethod
    def shader(cls, nonconvex=False):
        if nonconvex in cls._shaders:
            return cls._shaders[nonconvex]
        info = gpu.types.GPUShaderCreateInfo()
        varyings = gpu.types.GPUStageInterfaceInfo("boolean_preview_interpolants")
        varyings.smooth("VEC3", "worldPosition")
        varyings.flat("INT", "operandIsland")
        info.vertex_in(0, "VEC3", "position")
        info.vertex_in(1, "FLOAT", "islandIndex")
        info.vertex_out(varyings)
        info.fragment_out(0, "VEC4", "fragColor")
        for kind, name in (("MAT4", "viewProjection"), ("VEC3", "cutterDelta"), ("INT", "surfaceKind"),
                           ("INT", "diagnosticMode")):
            info.push_constant(kind, name)
        info.typedef_source("struct BooleanCutSettings { vec4 originAndEpsilon; vec4 inverseAndSide; };")
        info.uniform_buf(0, "BooleanCutSettings", "cutSettings")
        samplers = (*cls._cutter_textures, "targetBvh", "targetTriangles")
        if nonconvex:
            samplers += tuple(cls._nonconvex_textures)
        for index, name in enumerate(samplers):
            info.sampler(index, "FLOAT_2D", name)
        info.vertex_source(_VERTEX)
        fragment = _SHADE + _FRAGMENT
        if nonconvex:
            fragment = "#define BOOLEAN_NONCONVEX\n" + fragment + _NONCONVEX
        info.fragment_source(fragment)
        cls._shaders[nonconvex] = gpu.shader.create_from_info(info)
        return cls._shaders[nonconvex]

    def __init__(self, tables, *, surface=None):
        self.nonconvex = "cutter_bvh" in tables
        self.surface = surface if surface is not None else SurfaceDisplay(tables, self.shader(self.nonconvex))
        textures = self._cutter_textures | self._nonconvex_textures if self.nonconvex else self._cutter_textures
        self.textures = {name: _texture(tables[key]) for name, key in textures.items()}
        params = (*tables["grid_origin"], float(tables["epsilon"]), *tables["grid_inverse"],
                  float(tables["grid_side"]))
        self.settings = gpu.types.GPUUniformBuf(gpu.types.Buffer("FLOAT", len(params), params))
        self.cutter = self.batch(tables["cutter_positions"], tables["cutter_triangles"][:, (0, 2, 1)],
                                 tables["cutter_membership"])
        self.cutter_start = Vector(tables["initial_cutter_location"])

    @property
    def world_matrix(self):
        return self.surface.world_matrix

    def batch(self, coordinates, corners, membership=None):
        if membership is None:
            membership = np.full(len(coordinates), -1.0, dtype="f4")
        return batch_for_shader(self.shader(self.nonconvex), "TRIS", {"position": coordinates, "islandIndex": membership},
                                indices=corners.tolist())

    def draw(self, projection, delta=(0.0, 0.0, 0.0), reference=None, diagnostic=False):
        shader = self.shader(self.nonconvex)
        shader.bind()
        for name, texture in self.textures.items():
            shader.uniform_sampler(name, texture)
        shader.uniform_sampler("targetBvh", self.surface.bvh)
        shader.uniform_sampler("targetTriangles", self.surface.triangles)
        shader.uniform_float("viewProjection", projection)
        shader.uniform_float("cutterDelta", delta)
        shader.uniform_block("cutSettings", self.settings)
        shader.uniform_int("diagnosticMode", int(diagnostic))
        if reference is not None:
            shader.uniform_int("surfaceKind", 2)
            reference.draw(shader)
        else:
            shader.uniform_int("surfaceKind", 0)
            self.surface.batch.draw(shader)
            shader.uniform_int("surfaceKind", 1)
            self.cutter.draw(shader)

    def draw_in_view(self, region_view, cutter_location):
        delta = self.surface.inverse_linear @ (cutter_location - self.cutter_start)
        _draw_in_view(lambda projection: self.draw(projection, delta),
                      region_view.perspective_matrix @ self.world_matrix)


class ReducedDisplay:
    """Draw the complete low-resolution worker mesh when convex shells cannot represent a cut."""

    _shader = None

    @classmethod
    def shader(cls):
        if cls._shader is None:
            info = gpu.types.GPUShaderCreateInfo()
            varyings = gpu.types.GPUStageInterfaceInfo("boolean_reduced_interpolants")
            varyings.smooth("VEC3", "worldPosition")
            info.vertex_in(0, "VEC3", "position")
            info.vertex_out(varyings)
            info.fragment_out(0, "VEC4", "fragColor")
            info.push_constant("MAT4", "viewProjection")
            info.push_constant("INT", "diagnosticMode")
            info.vertex_source("""
void main() {
    worldPosition = position;
    gl_Position = viewProjection * vec4(position, 1.0);
}
""")
            info.fragment_source(_SHADE + """
void main() {
    fragColor = diagnosticMode != 0 ? vec4(0.45, 0.65, 0.85, 1.0) : shadeSurface(worldPosition);
}
""")
            cls._shader = gpu.shader.create_from_info(info)
        return cls._shader

    def __init__(self, tables, world_matrix):
        self.world_matrix = world_matrix
        self.batch = batch_for_shader(
            self.shader(), "TRIS", {"position": tables["fallback_positions"]},
            indices=tables["fallback_triangles"].tolist())

    def draw(self, projection, diagnostic=False):
        shader = self.shader()
        shader.bind()
        shader.uniform_float("viewProjection", projection)
        shader.uniform_int("diagnosticMode", int(diagnostic))
        self.batch.draw(shader)

    def draw_in_view(self, region_view, _cutter_location):
        _draw_in_view(self.draw, region_view.perspective_matrix @ self.world_matrix)
