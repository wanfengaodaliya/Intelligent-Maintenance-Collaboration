"""Minimum independent validation of the Markov network-state engine.

Replicates the exact RNG seeding used by RuntimeFactory and drives the
project's own MarkovNetworkModel over a simulated window using the
production configuration. Reports observed steady-state occupation and
transition counts versus the theoretical stationary distribution.

Does NOT require Toxiproxy, MQTT broker, or Scheduler - it exercises the
Markov state-generation layer only. Run:

    python verification/minimal_markov_validation.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from controller.config_loader import ConfigLoader  # noqa: E402
from controller.runtime_factory import resolve_link_seed  # noqa: E402
from domain.enums import NetworkState  # noqa: E402
from plugins.markov.model import MarkovNetworkModel  # noqa: E402

WARMUP_TICKS = 60    # discard transients
WINDOW_TICKS = 600   # 10 minutes at update_interval_seconds=1


def stationary_distribution(matrix):
    n = len(matrix)
    pi = [1.0 / n] * n
    for _ in range(50000):
        nxt = [sum(pi[i] * matrix[i][j] for i in range(n)) for j in range(n)]
        if max(abs(nxt[j] - pi[j]) for j in range(n)) < 1e-15:
            return nxt
        pi = nxt
    return pi


def main() -> None:
    config = ConfigLoader().load(CONFIG_DIR)
    names = [s.value for s in config.transition.states]

    # Build one independently-seeded model per production link, as RuntimeFactory does.
    models, link_ids = [], []
    for link in config.links:
        seed = resolve_link_seed(
            config.experiment.global_seed, link.link_id, link.seed_offset
        )
        rng = random.Random(seed)
        models.append(MarkovNetworkModel(config.transition.states,
                                         config.transition.matrix, rng))
        link_ids.append(link.link_id)

    nlinks = len(models)
    print(f"loaded config: {nlinks} links, global_seed={config.experiment.global_seed}")
    print(f"initial_state={config.controller.initial_state}, "
          f"tick_interval={config.controller.update_interval_seconds}s, "
          f"window={WINDOW_TICKS * config.controller.update_interval_seconds:.0f}s")

    state = [config.controller.initial_state] * nlinks
    transitions = Counter()
    per_link = {lid: Counter() for lid in link_ids}

    for tick in range(WARMUP_TICKS + WINDOW_TICKS):
        for i, model in enumerate(models):
            s2 = model.next_state(state[i])
            if tick >= WARMUP_TICKS:
                transitions[(state[i], s2)] += 1
                per_link[link_ids[i]][s2] += 1
            state[i] = s2

    observed = Counter()
    from_totals = Counter()
    for (f, t), c in transitions.items():
        observed[t] += c
        from_totals[f] += c
    total = sum(observed.values())
    occ = {s: observed[s] / total for s in names}

    theo = stationary_distribution([list(r) for r in config.transition.matrix])

    print("\n================ 稳态分布对照 ================")
    print(f"{'状态':<14}{'理论稳态':>10}{'实测占比':>10}{'绝对偏差':>10}")
    for i, name in enumerate(names):
        print(f"{name:<14}{theo[i]:>10.4f}{occ[name]:>10.4f}"
              f"{abs(theo[i]-occ[name]):>10.4f}")

    print("\n================ 实测转移计数(每次横转移计数/占源行比) ================")
    for f in names:
        cells = []
        for t in names:
            c = transitions.get((NetworkState(f), NetworkState(t)), 0)
            tot = from_totals.get(NetworkState(f), 0)
            cells.append(f"{c}({c/tot:.2f})" if tot else f"{c}(-)")
        print(f"从{f:<14}  " + "  ".join(f"{c:>11}" for c in cells))

    disc = [per_link[lid][NetworkState.DISCONNECTED] for lid in link_ids]
    print("\n================ DISCONNECTED 驻留(600 tick 窗口, 按链路) ================")
    print(f"链路数={len(disc)}, 均值={sum(disc)/len(disc):.1f} ticks/链路, "
          f"最小={min(disc)}, 最大={max(disc)}")

    out = {
        "links": nlinks,
        "warmup_ticks": WARMUP_TICKS,
        "window_ticks": WINDOW_TICKS,
        "tick_interval_s": config.controller.update_interval_seconds,
        "global_seed": config.experiment.global_seed,
        "states": names,
        "theoretical_stationary": [round(x, 6) for x in theo],
        "observed_occupation": {k: round(v, 6) for k, v in occ.items()},
        "absolute_error": {k: round(abs(theo[names.index(k)] - v), 6)
                           for k, v in occ.items()},
        "total_transitions": WINDOW_TICKS * nlinks,
    }
    dump = ROOT / "verification" / "minimal_markov_results.json"
    dump.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {dump}")


if __name__ == "__main__":
    main()