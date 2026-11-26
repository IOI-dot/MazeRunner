# MazeRunner

Three-layer maze router for CSCE 3304 Digital Design II, Project 2, Option 4.

## Requirements

Python 3.10 or newer. Uses the Python standard library only.

## Features

- Read grid dimensions, via cost, blocks, and net pins.
- Route branches using a full Dijkstra search from the existing tree.
- Follow the traceback order: North, East, South, West, Up, Down.
- Connect multiple pins in file order.
- Enforce layer directions, blocked points, net ownership, and via spacing.
- Print branch coordinates, wirelength, and via count for each net.

## Usage

Run commands from the folder containing `run.py`.

Route a case:

```bash
python3 run.py route tests/maze/visible/open.txt
```

Inspect an input:

```bash
python3 run.py inspect tests/maze/visible/open.txt
```

Show grid moves from a point `(x, y, layer)`:

```bash
python3 run.py moves tests/maze/visible/open.txt 1 1 1
```

The `moves` command checks directions, boundaries, and blocks. Net ownership and
via spacing are applied during routing.

## Core tests

```bash
python3 grade.py maze --sub . --only core
```

The public core cases are `open`, `block`, and `tree`. All three pass with exact
matches to the supplied expected output.

## Files

- `run.py`: input parser, grid model, search, traceback, and routing output.
- `grade.py`: unchanged supplied project grader.
- `tests/`: unchanged supplied public cases and expected results.
- `docs/Option4_Handout.pdf`: supplied project handout.

## Limitations

Nets are routed once in file order. Rip-up and re-route, SVG drawing, and
alternative-method experiments are not implemented. Congested multi-net cases
may fail when a later net cannot be connected.
