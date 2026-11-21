"""Per-commit cost breakdown of a selector output against answers (dev tool).

Usage: python tools/breakdown.py <data dir> <answers.json> <output.jsonl> [meta.json]
"""

import json
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from selector.pack import pack  # noqa: E402


def main():
    data_dir, answers_path, out_path = sys.argv[1:4]
    meta = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
    tests = json.load(open(f"{data_dir}/tests.json"))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    answers = json.load(open(answers_path))["commits"]
    rows = []
    by_kind = defaultdict(lambda: [0, 0, 0, 0])
    total = oracle_total = 0
    misses = 0
    for line in open(out_path):
        obj = json.loads(line)
        cid = obj["commit"]
        if cid not in answers:
            continue
        workers = obj["workers"]
        selected = {n for w in workers for n in w}
        cost = 0
        if any(workers):
            cost = max(sum(ms[n] for n in w) for w in workers if w) + 2000 * sum(1 for w in workers if w)
        failing = set(answers[cid]["failing"])
        miss = bool(failing - selected)
        _, oracle = pack(sorted(failing), ms)
        kind = answers[cid].get("kind") or meta.get(cid, {}).get("kind", "?")
        rows.append((cost - oracle + (300000 if miss else 0), cid, kind, cost, oracle, miss, len(selected), len(failing)))
        total += cost + (300000 if miss else 0)
        oracle_total += oracle
        misses += miss
        k = by_kind[kind]
        k[0] += 1
        k[1] += cost + (300000 if miss else 0)
        k[2] += oracle
        k[3] += miss
    rows.sort(reverse=True)
    print(f"total {total / 1000:.1f}s  oracle {oracle_total / 1000:.1f}s  misses {misses}  commits {len(rows)}")
    for kind, (n, c, o, m) in sorted(by_kind.items()):
        print(f"  {kind:10} n={n:3} cost={c / 1000:8.1f} oracle={o / 1000:7.1f} misses={m}")
    print("worst excess:")
    for excess, cid, kind, cost, oracle, miss, nsel, nfail in rows[:25]:
        print(f"  {cid} {kind:9} excess={excess / 1000:7.2f} cost={cost / 1000:6.2f} oracle={oracle / 1000:6.2f} "
              f"{'MISS ' if miss else ''}selected={nsel} failing={nfail}")


if __name__ == "__main__":
    main()
