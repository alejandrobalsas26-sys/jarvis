"""tools/schematic_compiler.py — Netlist-to-SVG circuit schematic compiler.

Renders JSON netlist descriptions to SVG via schemdraw with a headless
matplotlib Agg backend (no display required).

Netlist JSON format:
    {
        "components": [
            {"type": "resistor", "label": "R1", "direction": "right"},
            {"type": "capacitor", "label": "C1", "direction": "down"},
            ...
        ]
    }

Supported component types: resistor, capacitor, inductor, diode, led,
voltage_source, current_source, ground, switch, transistor_npn,
transistor_pnp, opamp, wire (default).
"""

import io

import matplotlib
matplotlib.use("Agg")  # headless — must precede all schemdraw/matplotlib imports

import schemdraw
import schemdraw.elements as elm


_DIRECTION_MAP = {
    "right": "right",
    "left":  "left",
    "up":    "up",
    "down":  "down",
}

_ELEMENT_MAP: dict[str, type] = {
    "resistor":        elm.Resistor,
    "r":               elm.Resistor,
    "capacitor":       elm.Capacitor,
    "c":               elm.Capacitor,
    "inductor":        elm.Inductor,
    "l":               elm.Inductor,
    "diode":           elm.Diode,
    "d":               elm.Diode,
    "led":             elm.LED,
    "voltage_source":  elm.SourceV,
    "v":               elm.SourceV,
    "current_source":  elm.SourceI,
    "i":               elm.SourceI,
    "ground":          elm.Ground,
    "gnd":             elm.Ground,
    "switch":          elm.Switch,
    "sw":              elm.Switch,
    "transistor_npn":  elm.BjtNpn,
    "transistor_pnp":  elm.BjtPnp,
    "opamp":           elm.Opamp,
    "wire":            elm.Line,
    "line":            elm.Line,
    "dot":             elm.Dot,
    "node":            elm.Dot,
}


def _draw_component(d: schemdraw.Drawing, component: dict) -> None:
    """Map a component dict to a schemdraw element and add it to the drawing."""
    comp_type = component.get("type", "wire").lower()
    label     = component.get("label", "")
    direction = _DIRECTION_MAP.get(component.get("direction", "right"), "right")

    elm_cls = _ELEMENT_MAP.get(comp_type, elm.Line)

    # Build the element with its direction
    element = elm_cls()
    match direction:
        case "right": element = element.right()
        case "left":  element = element.left()
        case "up":    element = element.up()
        case "down":  element = element.down()

    if label:
        element = element.label(label)

    d.add(element)


def compile_netlist_to_svg(netlist_json: dict) -> str:
    """Render a netlist dict to an SVG string.

    Args:
        netlist_json: dict with a "components" list, each entry having
                      "type", optional "label", and optional "direction".

    Returns:
        UTF-8 SVG string suitable for direct injection into innerHTML.
    """
    components = netlist_json.get("components", [])

    with schemdraw.Drawing(show=False) as d:
        for component in components:
            _draw_component(d, component)

    buf = io.BytesIO()
    d.save(buf, fmt="svg")
    return buf.getvalue().decode("utf-8")
