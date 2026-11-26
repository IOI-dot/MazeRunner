import argparse
import heapq
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


def routing_moves(grid, point, forbidden, via_keepout):
    # Keep other nets and unconnected pins out of the branch.
    moves = []
    for neighbour, cost in legal_moves(grid, point):
        if neighbour in forbidden:
            continue

        # Check spacing only for vias on the same layer cut.
        if point[2] != neighbour[2]:
            cut = min(point[2], neighbour[2])
            if (point[0], point[1], cut) in via_keepout:
                continue

        moves.append((neighbour, cost))
    return moves


def search(grid, tree, forbidden, via_keepout):
    # Every point in the existing tree starts with cost zero.
    labels = {point: 0 for point in tree}
    queue = [(0, point) for point in tree]
    heapq.heapify(queue)

    # Label every reachable point using Dijkstra's algorithm.
    while queue:
        distance, point = heapq.heappop(queue)
        if distance != labels[point]:
            continue

        for neighbour, cost in routing_moves(grid, point, forbidden, via_keepout):
            new_distance = distance + cost
            if new_distance < labels.get(neighbour, float("inf")):
                labels[neighbour] = new_distance
                heapq.heappush(queue, (new_distance, neighbour))

    return labels


def traceback(grid, target, labels, forbidden, via_keepout):
    branch = [target]
    point = target

    # Follow decreasing labels until the existing tree is reached.
    while labels[point] != 0:
        for neighbour, cost in routing_moves(grid, point, forbidden, via_keepout):
            if neighbour in labels and labels[neighbour] + cost == labels[point]:
                branch.append(neighbour)
                point = neighbour
                break
        else:
            raise ValueError("Traceback could not reach the existing tree.")

    # Output runs from the tree to the new pin.
    branch.reverse()
    return branch


def route_nets(grid):
    routes = []
    occupied = set()
    via_keepout = set()
    pin_owners = {}
    for net in grid.nets:
        for pin in net.pins:
            pin_owners[pin] = net.name

    # Route nets and their pins in file order.
    for net in grid.nets:
        tree = {net.pins[0]}
        connected = {net.pins[0]}
        branches = []
        other_pins = {pin for pin, owner in pin_owners.items() if owner != net.name}

        for target in net.pins[1:]:
            # Only the current target may be entered before it is connected.
            future_pins = set(net.pins) - connected - {target}
            forbidden = occupied | other_pins | future_pins
            labels = search(grid, tree, forbidden, via_keepout)
            if target not in labels:
                raise ValueError(f"No route found for net {net.name} to pin {target}.")

            branch = traceback(grid, target, labels, forbidden, via_keepout)
            branches.append(branch)
            tree.update(branch)
            connected.add(target)

        occupied.update(tree)
        routes.append((net.name, branches))

        # Reserve neighbouring via positions for the following nets.
        # Vias of the same net may be close together or stacked.
        for branch in branches:
            for start, end in zip(branch, branch[1:]):
                if start[2] != end[2]:
                    x, y = start[:2]
                    cut = min(start[2], end[2])
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            via_keepout.add((x + dx, y + dy, cut))

    return routes


def format_routes(routes):
    # Count wire steps and vias separately for each net.
    lines = []
    for name, branches in routes:
        wirelength = 0
        vias = 0
        lines.append(f"NET {name}")

        for branch in branches:
            lines.append("BRANCH")
            for x, y, layer in branch:
                lines.append(f"{x} {y} {layer}")
            for start, end in zip(branch, branch[1:]):
                if start[2] == end[2]:
                    wirelength += 1
                else:
                    vias += 1

        lines.append("END")
        lines.append(f"WL {wirelength} VIAS {vias}")

    return "\n".join(lines)


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

    route_command = commands.add_parser("route", help="Route nets and print their branches")
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
            print(format_routes(route_nets(grid)))
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
