# MazeRunner

Three-layer maze routing project for CSCE 3304 Digital Design II, Option 4.

## Requirements

Python 3.10 or newer. Uses the Python standard library only.

## Features

- Read grid dimensions, via cost, blocked rectangles, and net pins.
- Support comments starting with `#`.
- Generate horizontal moves on M1 and M3, vertical moves on M2, and vias between adjacent layers.
- Check grid boundaries and blocked points.

Routing search, traceback, net occupancy, and via spacing are not implemented yet.

## Usage

Show an input summary:

```bash
python3 run.py inspect tests/maze/visible/open.txt
```

Show available grid moves from a point `(x, y, layer)`:

```bash
python3 run.py moves tests/maze/visible/open.txt 1 1 1
```

## Files

- `run.py`: input parser and grid movement rules.
- `grade.py`: supplied project grader.
- `tests/`: supplied public test cases and expected results.
- `docs/Option4_Handout.pdf`: project handout.

## Grading

```bash
python3 grade.py maze --sub . --only core
```
