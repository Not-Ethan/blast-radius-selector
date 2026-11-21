"""Load the coverage maps built from the pinned commit (see tools/build_maps.py)."""

from __future__ import annotations

import gzip
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(os.path.dirname(HERE), "maps", "coverage.json.gz")
CLASSINFO = os.path.join(os.path.dirname(HERE), "maps", "classinfo.json.gz")
SETUP_COST = os.path.join(os.path.dirname(HERE), "maps", "setup_cost.json")


class Maps:
    def __init__(self, path: str = DEFAULT):
        with gzip.open(path, "rt") as handle:
            data = json.load(handle)
        self.tests: list[str] = data["tests"]
        self.index = {node: i for i, node in enumerate(self.tests)}
        self.cost: list[float] = [c if c is not None else 1.0 for c in data["cost"]]
        self.wall: list[float] = [w if w is not None else 1.0 for w in data.get("wall", data["cost"])]
        self.vcpu: list = data.get("vcpu") or [None] * len(self.tests)
        self._sets_hex: list[str] = data["sets"]
        self._sets: dict[int, int] = {}
        self.func = data["func"]
        self.mod = data["mod"]
        self.entry = data["entry"]
        self.opens = data["opens"]
        self.lists = data["lists"]
        self.imports = data["imports"]
        self.inst = data.get("inst", {})
        self.all_mask = (1 << len(self.tests)) - 1
        self.classes = {}
        self.dynamic = []
        self.test_spans = {}
        if os.path.exists(CLASSINFO):
            with gzip.open(CLASSINFO, "rt") as handle:
                info = json.load(handle)
            self.dynamic = info.pop("__dynamic__", [])
            self.test_spans = info.pop("__testspans__", {})
            self.classes = info
        self._children = None
        self.setup_cost = {}
        if os.path.exists(SETUP_COST):
            with open(SETUP_COST) as handle:
                self.setup_cost = json.load(handle)

    def cpu_estimate(self, index: int):
        return self.vcpu[index]

    def inst_mask(self, key) -> int:
        return self.mask(self.inst.get(key))

    def descendants(self, key) -> set[str]:
        if self._children is None:
            children = {}
            for k, v in self.classes.items():
                for base in v.get("bases", []):
                    children.setdefault(base, []).append(k)
            self._children = children
        out = {key}
        stack = [key]
        while stack:
            for child in self._children.get(stack.pop(), []):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    def mask(self, k) -> int:
        if k is None:
            return 0
        value = self._sets.get(k)
        if value is None:
            value = self._sets[k] = int(self._sets_hex[k], 16)
        return value

    def lines_mask(self, table: dict, path: str, lines) -> int:
        bucket = table.get(path)
        if not bucket:
            return 0
        mask = 0
        for line in lines:
            k = bucket.get(str(line))
            if k is not None:
                mask |= self.mask(k)
        return mask

    def func_mask(self, path, lines) -> int:
        return self.lines_mask(self.func, path, lines)

    def mod_mask(self, path, lines) -> int:
        return self.lines_mask(self.mod, path, lines)

    def entry_mask(self, path, firstlines) -> int:
        return self.lines_mask(self.entry, path, firstlines)

    def open_mask(self, path) -> int:
        return self.mask(self.opens.get(path))

    def import_mask(self, path) -> int:
        return self.mask(self.imports.get(path))

    def list_mask(self, directory) -> int:
        return self.mask(self.lists.get(directory))

    @staticmethod
    def tests_of(mask: int) -> list[int]:
        out = []
        while mask:
            low = mask & -mask
            out.append(low.bit_length() - 1)
            mask ^= low
        return out
