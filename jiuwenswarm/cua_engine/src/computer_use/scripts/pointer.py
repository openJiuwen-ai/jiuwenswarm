"""Strict compiler for the shared cursor SVG profile and motion keyframes."""

import copy
import hashlib
import logging
import math
import re
import sys
from pathlib import Path

from computer_use._json import loads as jsonLoads

from ..observation.formatting import number_string
from ..paths import repository_root

NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"


def require(condition, message):
    if not condition:
        raise ValueError("Agent pointer asset: " + message)


def parse_xml(source):
    source = re.sub(r"<(title|desc)>[^<]*</\1>", "", re.sub(r"<!--[\s\S]*?-->", "", source)).strip()
    roots, stack, at = [], [], 0
    while at < len(source):
        match = re.compile(r"\s*<([^<>]+)>").match(source, at)
        require(match is not None, "unsupported XML/text content")
        at = match.end()
        content = match[1]
        if content.startswith("/"):
            require(stack and stack.pop()["tag"] == content[1:].strip(), "mismatched closing tag")
            continue
        start = re.match(r"([A-Za-z][\w-]*)\b", content, re.ASCII)
        require(start is not None, "unsupported XML declaration")
        node = dict(tag=start[1], attrs={}, children=[])
        self_closing = content.endswith("/")
        rest = content[start.end():].removesuffix("/")
        while rest.strip():
            attr = re.match(r'\s+([A-Za-z][\w:-]*)\s*=\s*"([^"&<>]*)"', rest, re.ASCII)
            require(attr is not None and attr[1] not in node["attrs"], "unsupported/duplicate SVG attribute")
            node["attrs"][attr[1]] = attr[2]
            rest = rest[attr.end():]
        (stack[-1]["children"] if stack else roots).append(node)
        if not self_closing:
            stack.append(node)
    require(not stack and len(roots) == 1, "expected one complete SVG")
    return roots[0]


def attributes(node, tag, names):
    require(node and node["tag"] == tag, f"expected <{tag}>")
    require(set(node["attrs"]) == set(names.split()), f"unsupported/missing attributes on <{tag}>")
    return node["attrs"]


def number(value):
    require(isinstance(value, str) and re.fullmatch(NUMBER, value.strip(), re.ASCII), f"invalid number {value}")
    result = float(value)
    require(math.isfinite(result), f"invalid number {value}")
    return result


def numeric(value, *, positive=False, unit=False):
    require(type(value) in (int, float) and math.isfinite(value), "expected finite number")
    require(not positive or value > 0, "expected positive number")
    require(not unit or 0 <= value <= 1, "expected number in [0,1]")
    return value


def color(value):
    require(isinstance(value, str) and re.fullmatch(r"#[0-9a-f]{6}", value, re.IGNORECASE), "expected #RRGGBB color")
    return value


def parse_path(d, closed=True):
    tokens = re.findall("[MLCZ]|" + NUMBER, d, re.ASCII)
    require(
        re.sub("[MLCZ]|" + NUMBER + r"|[\s,]+", "", d, flags=re.ASCII) == "",
        "only absolute M/L/C/Z paths are supported",
    )
    i, commands = 0, []
    while i < len(tokens):
        op = tokens[i]
        i += 1
        count = {"M": 2, "L": 2, "C": 6, "Z": 0}.get(op)
        require(
            count is not None and i + count <= len(tokens),
            "incomplete path command (repeat command letters explicitly)",
        )
        commands.append(dict(op=op, values=[number(value) for value in tokens[i: i + count]]))
        i += count
    require(
        commands and commands[0]["op"] == "M" and (not closed or commands[-1]["op"] == "Z"),
        "expected a path starting with M and a closed silhouette",
    )
    return commands


def motion_schema(m):

    def keys(value, expected):
        require(isinstance(value, dict) and set(value) == set(expected.split()), "unsupported/missing motion keys")

    def array(value, *, length=None, positive=False, unit=False, times=False):
        require(isinstance(value, list) and (length is None or len(value) == length), "invalid motion tuple")
        for x in value:
            numeric(x, positive=positive, unit=unit or times)
        if times:
            require(
                len(value) >= 2 and value[0] == 0 and value[-1] == 1 and all(a < b for a, b in zip(value, value[1:])),
                "invalid animation keyframe times",
            )

    keys(m, "version hotspot move appearMs idle press ripple drag reducedMotion")
    require(type(m["version"]) in (int, float) and m["version"] == 1, "unsupported motion version")
    array(m["hotspot"], length=2)
    numeric(m["appearMs"], positive=True)
    groups = {
        "move": "minMs maxMs pointsPerSecond easing",
        "idle": "delayMs opacity fadeMs wakeMs",
        "press": "durationMs scales times doubleDurationMs doubleScales doubleTimes",
        "ripple": "durationMs fromRadius toRadius fromOpacity color width",
        "drag": "minMs maxMs pointsPerSecond scale transitionMs",
        "reducedMotion": "pressedColor flashMs holdUntil",
    }
    for group, fields in groups.items():
        keys(m[group], fields)
        for key, value in m[group].items():
            if key in ("color", "pressedColor"):
                color(value)
            elif key == "easing":
                array(value, length=4, unit=True)
            elif key in ("times", "doubleTimes"):
                array(value, times=True)
            elif key in ("scales", "doubleScales"):
                array(value, positive=True)
            elif key in ("opacity", "fromOpacity", "holdUntil"):
                numeric(value, unit=True)
            else:
                numeric(value, positive=True)
    require(
        m["move"]["minMs"] <= m["move"]["maxMs"]
        and m["drag"]["minMs"] <= m["drag"]["maxMs"]
        and len(m["press"]["scales"]) == len(m["press"]["times"])
        and len(m["press"]["doubleScales"]) == len(m["press"]["doubleTimes"])
        and m["ripple"]["fromRadius"] <= m["ripple"]["toRadius"],
        "Invalid animation ranges or keyframes",
    )
    # Zod object output follows schema order, regardless of the source key order.
    return {
        "version": m["version"],
        "hotspot": list(m["hotspot"]),
        "move": {key: copy.deepcopy(m["move"][key]) for key in groups["move"].split()},
        "appearMs": m["appearMs"],
        **{
            group: {key: copy.deepcopy(m[group][key]) for key in fields.split()}
            for group, fields in groups.items()
            if group != "move"
        },
    }


def allowed(node, required, optional=""):
    require(
        set(required.split()) <= set(node["attrs"]) <= set((required + " " + optional).split()),
        f"unsupported/missing attributes on <{node['tag']}>",
    )
    return node["attrs"]


def compile_assets(svg, data):
    root = parse_xml(svg)
    require(root["tag"] == "svg", "expected svg root")
    a = allowed(root, "xmlns width height viewBox fill")
    require(a["xmlns"] == "http://www.w3.org/2000/svg" and a["fill"] == "none", "expected SVG namespace and fill=none")
    size = [number(a["width"]), number(a["height"])]
    box = [number(v) for v in a["viewBox"].split()]
    require(all(v > 0 for v in size) and box == [0, 0, *size], "viewBox must match logical size with origin 0,0")
    require(bool(root["children"]), "expected defs")
    defs, *nodes = root["children"]
    attributes(defs, "defs", "")
    paths, gradients, definitions, ids = [], [], {}, set()

    def identity(value):
        require(value and value not in ids, "missing/duplicate SVG id")
        ids.add(value)

    def reference(value, kind):
        match = re.fullmatch(r"#([\w-]+)", value, re.ASCII)
        require(match is not None, "only local SVG references are supported")
        found = definitions.get(match[1])
        require(found and found["kind"] == kind, f"invalid {kind} reference {value}")
        return found

    def url(value, kind):
        match = re.fullmatch(r"url\((#[\w-]+)\)", value, re.ASCII)
        require(match is not None, f"invalid {kind} URL")
        return reference(match[1], kind)

    for node in defs["children"]:
        key = node["attrs"].get("id")
        identity(key)
        if node["tag"] == "path":
            v = attributes(node, "path", "id d")
            require(not node["children"], "unexpected path children")
            definitions[key] = dict(kind="path", index=len(paths))
            paths.append(parse_path(v["d"]))
        elif node["tag"] in ("linearGradient", "radialGradient"):
            radial = node["tag"] == "radialGradient"
            v = attributes(node, node["tag"], "id gradientUnits " + ("cx cy r" if radial else "x1 y1 x2 y2"))
            require(v["gradientUnits"] == "userSpaceOnUse", "gradient coordinates must use userSpaceOnUse")
            stops = []
            for stop in node["children"]:
                st = allowed(stop, "stop-color", "offset stop-opacity")
                require(stop["tag"] == "stop" and not stop["children"], "expected gradient stop")
                stops.append(
                    dict(
                        color=color(st["stop-color"]),
                        opacity=numeric(number(st.get("stop-opacity", "1")), unit=True),
                        offset=numeric(number(st.get("offset", "0")), unit=True),
                    )
                )
            require(
                len(stops) >= 2
                and stops[0]["offset"] == 0
                and stops[-1]["offset"] == 1
                and all(a["offset"] <= b["offset"] for a, b in zip(stops, stops[1:])),
                "invalid gradient stops",
            )
            points = [number(v[k]) for k in ("cx cy r" if radial else "x1 y1 x2 y2").split()]
            require(not radial or points[2] > 0, "invalid gradient radius")
            definitions[key] = dict(kind="gradient", index=len(gradients))
            gradients.append(dict(radial=radial, points=points, stops=stops))
        elif node["tag"] == "clipPath":
            attributes(node, "clipPath", "id")
            require(
                len(node["children"]) == 1
                and node["children"][0]["tag"] == "use"
                and not node["children"][0]["children"],
                "clip must reference one path",
            )
            v = attributes(node["children"][0], "use", "href")
            definitions[key] = dict(kind="clip", path=reference(v["href"], "path")["index"])
        elif node["tag"] == "filter":
            f = attributes(node, "filter", "id x y width height color-interpolation-filters")
            expected_filter = {
                "x": "-40%",
                "y": "-30%",
                "width": "180%",
                "height": "175%",
                "color-interpolation-filters": "sRGB",
            }
            require(
                all(f[k] == v for k, v in expected_filter.items()) and len(node["children"]) == 1,
                "unsupported shadow filter",
            )
            st = attributes(node["children"][0], "feDropShadow", "dx dy stdDeviation flood-color flood-opacity")
            require(not node["children"][0]["children"], "unexpected shadow children")
            definitions[key] = dict(
                kind="shadow",
                dx=number(st["dx"]),
                dy=number(st["dy"]),
                radius=numeric(number(st["stdDeviation"]), positive=True),
                color=color(st["flood-color"]),
                opacity=numeric(number(st["flood-opacity"]), unit=True),
            )
        else:
            require(False, "unsupported definition " + node["tag"])

    def paint(value):
        if not value or value == "none":
            return None
        return dict(gradient=url(value, "gradient")["index"]) if value.startswith("url(") else dict(color=color(value))

    def render(node):
        if node["tag"] == "g":
            v = attributes(node, "g", "clip-path")
            return dict(clip=url(v["clip-path"], "clip")["path"], children=[render(n) for n in node["children"]])
        require(node["tag"] in ("use", "path"), "unsupported drawing " + node["tag"])
        v = allowed(
            node,
            "href" if node["tag"] == "use" else "d",
            "id fill stroke stroke-opacity stroke-width stroke-linejoin stroke-linecap filter",
        )
        require(not node["children"], "unexpected drawing children")
        if v.get("id"):
            identity(v["id"])
        if node["tag"] == "use":
            path = reference(v["href"], "path")["index"]
        else:
            path = len(paths)
            paths.append(parse_path(v["d"], False))
        fill, stroke = paint(v.get("fill")), paint(v.get("stroke"))
        require(fill or stroke, "drawing must have a fill or stroke")
        require(not v.get("stroke-linejoin") or v["stroke-linejoin"] == "round", "unsupported line join")
        require(not v.get("stroke-linecap") or v["stroke-linecap"] == "round", "unsupported line cap")
        return dict(
            path=path,
            fill=fill,
            stroke=stroke,
            opacity=numeric(number(v.get("stroke-opacity", "1")), unit=True),
            width=numeric(number(v.get("stroke-width", "1")), positive=True),
            roundJoin=v.get("stroke-linejoin") == "round",
            roundCap=v.get("stroke-linecap") == "round",
            shadow=url(v["filter"], "shadow") if v.get("filter") else None,
        )

    drawing = [render(node) for node in nodes]
    motion = motion_schema(jsonLoads(data) if isinstance(data, str) else data)
    require(all(0 <= v <= size[i] for i, v in enumerate(motion["hotspot"])), "hotspot outside viewBox")
    shape = reference("#shape", "path")["index"]
    require("body" in ids, "missing #body")
    return dict(
        size=size, path=paths[shape], shape=shape, paths=paths, gradients=gradients, drawing=drawing, motion=motion
    )


def swift_source(asset, digest):
    n = number_string
    m = asset["motion"]

    def point(x, y):
        return f"CGPoint(x: {n(x)}, y: {n(y)})"

    def rgb(value, alpha=1):
        return (
            f"CGColor(srgbRed: {int(value[1:3], 16)}/255, green: {int(value[3:5], 16)}/255, "
            f"blue: {int(value[5:], 16)}/255, alpha: {n(alpha)})"
        )

    outline_width = 0
    for item in asset["drawing"]:
        if item.get("path") == asset["shape"] and item.get("stroke"):
            outline_width = max(outline_width, item["width"])
    values = dict(
        size=f"CGSize(width: {n(asset['size'][0])}, height: {n(asset['size'][1])})",
        hotspot=point(*m["hotspot"]),
        outlineWidth=f"CGFloat({n(outline_width)})",
    )
    values.update(easing="[" + ", ".join(map(n, m["move"]["easing"])) + "] as [Float]", appear=n(m["appearMs"] / 1000))
    for group, entries in m.items():
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            if key == "easing":
                continue
            name = group + key[0].upper() + key[1:].removesuffix("Ms")
            values[name] = (
                rgb(value)
                if isinstance(value, str)
                else "[" + ", ".join(map(n, value)) + "] as [Double]"
                if isinstance(value, list)
                else f"Double({n(value / 1000 if key.endswith('Ms') else value)})"
            )

    def path_commands(commands):
        result = []
        for command in commands:
            op, v = command["op"], command["values"]
            result.append(
                "path.closeSubpath()"
                if op == "Z"
                else (
                    f"path.addCurve(to: {point(v[4], v[5])}, "
                    f"control1: {point(v[0], v[1])}, control2: {point(v[2], v[3])})"
                )
                if op == "C"
                else f"path.{'move' if op == 'M' else 'addLine'}(to: {point(*v)})"
            )
        return result

    drawing = []

    def emit(line):
        drawing.append("        " + line)

    def draw_paint(paint):
        if "color" in paint:
            emit(f"context.setFillColor({rgb(paint['color'])})")
            emit("context.fill(CGRect(x: 0, y: 0, width: size.width, height: size.height))")
        else:
            index = paint["gradient"]
            g = asset["gradients"][index]
            p = g["points"]
            args = (
                f"startCenter: {point(*p[:2])}, startRadius: 0, endCenter: {point(*p[:2])}, endRadius: {n(p[2])}"
                if g["radial"]
                else f"start: {point(*p[:2])}, end: {point(*p[2:])}"
            )
            emit(
                (
                    f"context.draw{'Radial' if g['radial'] else 'Linear'}Gradient(gradient{index}"
                    f"(), {args}, options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])"
                )
            )

    def draw(node):
        emit("context.saveGState()")
        if "children" in node:
            emit(f"context.addPath(path{node['clip']}()); context.clip()")
            for child in node["children"]:
                draw(child)
        else:
            if node["shadow"]:
                s = node["shadow"]
                emit(
                    (
                        f"context.setShadow(offset: CGSize(width: {n(s['dx'])}, height: {n(-s['dy'])}), "
                        f"blur: {n(s['radius'])}, color: {rgb(s['color'], s['opacity'])})"
                    )
                )
                emit("context.beginTransparencyLayer(auxiliaryInfo: nil)")
            if node["fill"]:
                emit(f"context.saveGState(); context.addPath(path{node['path']}()); context.clip()")
                draw_paint(node["fill"])
                emit("context.restoreGState()")
            if node["stroke"]:
                emit(f"context.saveGState(); context.addPath(path{node['path']}())")
                emit(
                    (
                        f"context.setLineWidth({n(node['width'])}); context.setLineJoin(."
                        f"{'round' if node['roundJoin'] else 'miter'}); context.setLineCap(."
                        f"{'round' if node['roundCap'] else 'butt'})"
                    )
                )
                emit(f"context.replacePathWithStrokedPath(); context.clip(); context.setAlpha({n(node['opacity'])})")
                draw_paint(node["stroke"])
                emit("context.restoreGState()")
            if node["shadow"]:
                emit("context.endTransparencyLayer()")
        emit("context.restoreGState()")

    for node in asset["drawing"]:
        draw(node)
    paths = "\n\n".join(
        f"    private static func path{i}() -> CGPath {{\n        let path = CGMutablePath()\n"
        + "\n".join("        " + s for s in path_commands(commands))
        + "\n        return path\n    }"
        for i, commands in enumerate(asset["paths"])
    )
    gradients = "\n\n".join(
        (
            f"    private static func gradient{i}() -> CGGradient {{\n        "
            f"CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,\n            "
            f"colors: ["
        )
        + ", ".join(rgb(s["color"], s["opacity"]) for s in g["stops"])
        + "] as CFArray,\n            locations: ["
        + ", ".join(n(s["offset"]) for s in g["stops"])
        + "])!\n    }"
        for i, g in enumerate(asset["gradients"])
    )
    return "".join(
        [
            "// Generated by computer-use-pointer. DO NOT EDIT.\n",
            f"// Source: assets/agent-pointer/{{cursor.svg,motion.json}}; sha256 {digest}"
            f"\nimport CoreGraphics\n\nenum AgentPointerAssets {{\n",
            "\n".join(f"    static let {k} = {v}" for k, v in values.items()),
            "\n\n    /// SVG uses y-down; the feedback path is relative to the native "
            "hotspot.\n    static func path() -> CGPath {\n        var transform = "
            "CGAffineTransform(a: 1, b: 0, c: 0, d: -1, tx: -hotspot.x, ty: "
            "hotspot.y)\n",
            f"        return path{asset['shape']}().copy(using: &transform)!\n",
            "    }\n\n    /// Draw the complete SVG into a size-matched y-up layer, "
            "including alpha.\n    static func draw(in context: CGContext) {\n        "
            "context.saveGState()\n        context.clip(to: CGRect(origin: .zero, "
            "size: size))\n        context.translateBy(x: 0, y: size.height); "
            "context.scaleBy(x: 1, y: "
            "-1)\n",
            "\n".join(drawing),
            "\n        context.restoreGState()\n    }\n\n",
            paths,
            "\n\n",
            gradients,
            "\n}\n",
        ]
    )


def generate_pointer_assets(check=False, root=None):
    root = Path(root) if root else repository_root()
    assets = root / "assets/agent-pointer"
    svg, motion_json = (
        (assets / "cursor.svg").read_text(encoding="utf-8"),
        (assets / "motion.json").read_text(encoding="utf-8"),
    )
    digest = hashlib.sha256((svg + "\0" + motion_json).encode()).hexdigest()
    source = swift_source(compile_assets(svg, motion_json), digest)
    output = root / "backends/macos/Sources/Preview/AgentPointerAssets.swift"
    old = output.read_text(encoding="utf-8") if output.exists() else None
    if check:
        require(old == source, "generated Swift is stale; run computer-use-pointer")
    elif old != source:
        output.write_text(source, encoding="utf-8")
    return output


def cli():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    output = generate_pointer_assets(check="--check" in sys.argv[1:])
    logging.getLogger(__name__).info("%s", output)


if __name__ == "__main__":
    cli()
