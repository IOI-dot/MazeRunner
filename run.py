import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


# A point is stored as (x, y, layer).
Point = tuple[int, int, int]


@dataclass
class Net:
    name: str
    pins: list[Point]


@dataclass
class Grid:
    width: int
    height: int
    via_cost: int
    blocked: set[Point]
    nets: list[Net]


def inside_grid(grid, point):
    # Check the coordinates and the layer number.
    x, y, layer = point
    return 0 <= x < grid.width and 0 <= y < grid.height and 1 <= layer <= 3


def read_input(filename):
    # Remove comments, then collect the input words.
    tokens = []
    for line in Path(filename).read_text(encoding="utf-8-sig").splitlines():
        tokens.extend(line.split("#", 1)[0].split())

    width = None
    height = None
    via_cost = None
    blocks = []
    nets = []
    index = 0

    while index < len(tokens):
        command = tokens[index]
        index += 1

        # Each command continues until the next word starting with a dot.
        values = []
        while index < len(tokens) and not tokens[index].startswith("."):
            values.append(tokens[index])
            index += 1

        if command == ".grid":
            # Read the grid size.
            if len(values) != 2 or width is not None:
                raise ValueError("Use .grid W H exactly once.")
            width, height = map(int, values)
            if width <= 0 or height <= 0:
                raise ValueError("Grid dimensions must be positive.")

        elif command == ".via_cost":
            # Read the cost of moving between two layers.
            if len(values) != 1 or via_cost is not None:
                raise ValueError("Use .via_cost V exactly once.")
            via_cost = int(values[0])
            if via_cost <= 0:
                raise ValueError("Via cost must be positive.")

        elif command == ".block":
            # Store the blocked rectangle.
            if len(values) != 5 or values[0] not in ("1", "2", "3", "*"):
                raise ValueError("Use .block L x1 y1 x2 y2, with L = 1, 2, 3 or *.")
            layer = values[0]
            x1, y1, x2, y2 = map(int, values[1:])
            blocks.append((layer, x1, y1, x2, y2))

        elif command == ".net":
            # Keep the pins in the same order as the input file.
            if len(values) < 5 or (len(values) - 1) % 2 != 0:
                raise ValueError("A net needs a name and at least two x y pairs.")
            name = values[0]
            if any(net.name == name for net in nets):
                raise ValueError(f"Duplicate net name: {name}")
            coordinates = list(map(int, values[1:]))
            pins = []
            for i in range(0, len(coordinates), 2):
                pins.append((coordinates[i], coordinates[i + 1], 1))
            nets.append(Net(name, pins))

        else:
            raise ValueError(f"Unknown input command: {command}")

    if width is None or via_cost is None:
        raise ValueError("The input needs .grid and .via_cost.")
    if not nets:
        raise ValueError("The input needs at least one net.")

    grid = Grid(width, height, via_cost, set(), nets)

    for layer, x1, y1, x2, y2 in blocks:
        if not (0 <= x1 <= x2 < width and 0 <= y1 <= y2 < height):
            raise ValueError("A block must be an ordered rectangle inside the grid.")

        # A star blocks the same rectangle on all three layers.
        layers = (1, 2, 3) if layer == "*" else (int(layer),)
        for current_layer in layers:
            for x in range(x1, x2 + 1):
                for y in range(y1, y2 + 1):
                    grid.blocked.add((x, y, current_layer))

    pin_owners = {}
    for net in nets:
        for pin in net.pins:
            if not inside_grid(grid, pin) or pin in grid.blocked:
                raise ValueError(f"Net {net.name} has a pin outside the grid or in a block.")
            if pin in pin_owners and pin_owners[pin] != net.name:
                raise ValueError("Different nets cannot share a pin point.")
            pin_owners[pin] = net.name

    return grid


def legal_moves(grid, point):
    # Check layer directions, boundaries and blocks.
    # Net occupancy and via spacing are not checked here.
    if not inside_grid(grid, point) or point in grid.blocked:
        return []

    x, y, layer = point
    candidates = []

    # Keep the order North, East, South, West, Up, Down.
    if layer == 2:
        candidates.append(((x, y + 1, layer), 1))
    if layer in (1, 3):
        candidates.append(((x + 1, y, layer), 1))
    if layer == 2:
        candidates.append(((x, y - 1, layer), 1))
    if layer in (1, 3):
        candidates.append(((x - 1, y, layer), 1))

    # A via changes one layer without changing x or y.
    if layer < 3:
        candidates.append(((x, y, layer + 1), grid.via_cost))
    if layer > 1:
        candidates.append(((x, y, layer - 1), grid.via_cost))

    moves = []
    for neighbour, cost in candidates:
        if inside_grid(grid, neighbour) and neighbour not in grid.blocked:
            moves.append((neighbour, cost))
    return moves


def show_summary(grid):
    # Display the input summary.
    print(f"Grid: {grid.width} x {grid.height}, 3 layers")
    print(f"Via cost: {grid.via_cost}")
    print(f"Blocked points: {len(grid.blocked)}")
    print(f"Nets: {len(grid.nets)}")
    for net in grid.nets:
        print(f"Net {net.name}: {len(net.pins)} pins {net.pins}")


def main():
    parser = argparse.ArgumentParser(description="MazeRunner: three-layer maze router")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_command = commands.add_parser("inspect", help="Read a file and show its contents")
    inspect_command.add_argument("file")

    moves_command = commands.add_parser("moves", help="Show moves allowed by the grid")
    moves_command.add_argument("file")
    moves_command.add_argument("x", type=int)
    moves_command.add_argument("y", type=int)
    moves_command.add_argument("layer", type=int, choices=(1, 2, 3))

    route_command = commands.add_parser("route", help="Reserved for the routing solver")
    route_command.add_argument("file")
    args = parser.parse_args()

    try:
        grid = read_input(args.file)
        if args.command == "inspect":
            show_summary(grid)
        elif args.command == "moves":
            point = (args.x, args.y, args.layer)
            if not inside_grid(grid, point) or point in grid.blocked:
                raise ValueError("Choose a starting point inside the grid and outside blocks.")
            for neighbour, cost in legal_moves(grid, point):
                x, y, layer = neighbour
                print(f"{x} {y} {layer} COST {cost}")
        else:
            # Routing search is not implemented yet.
            raise NotImplementedError("Routing is not implemented yet. Use inspect or moves.")
    except (OSError, ValueError, NotImplementedError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
