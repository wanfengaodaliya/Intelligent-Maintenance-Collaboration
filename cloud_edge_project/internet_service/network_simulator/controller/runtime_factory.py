"""Build initial V3 link runtimes and their private state engines."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import random
import time

from controller.config_loader import ApplicationConfig, ResolvedLinkConfig
from domain.enums import ExperimentMode, NetworkState
from domain.models import LinkRuntime
from plugins.markov.fixed import FixedNetworkModel
from plugins.markov.mapper import NetworkStateMapper
from plugins.markov.model import MarkovNetworkModel
from plugins.markov.plugin import LinkStateEngine, MarkovPlugin


SEED_OFFSET_MODULUS = 1_000_000


def stable_seed_offset(link_id: str) -> int:
    digest = hashlib.sha256(link_id.encode("utf-8")).digest()
    return int.from_bytes(digest, byteorder="big") % SEED_OFFSET_MODULUS


def resolve_link_seed(
    global_seed: int,
    link_id: str,
    seed_offset: int | None,
) -> int:
    offset = stable_seed_offset(link_id) if seed_offset is None else seed_offset
    return global_seed + offset


@dataclass(frozen=True, slots=True)
class RuntimeAssembly:
    runtimes: tuple[LinkRuntime, ...]
    plugin: MarkovPlugin


class RuntimeFactory:
    def __init__(self, config: ApplicationConfig) -> None:
        self._config = config

    def build(self, timestamp_ns: int | None = None) -> RuntimeAssembly:
        created_at_ns = time.time_ns() if timestamp_ns is None else timestamp_ns
        if created_at_ns < 0:
            raise ValueError("timestamp_ns cannot be negative")

        runtimes: list[LinkRuntime] = []
        engines: dict[str, LinkStateEngine] = {}
        for link in self._config.links:
            runtime, engine = self._build_link(link, created_at_ns)
            runtimes.append(runtime)
            engines[link.link_id] = engine
        plugin = MarkovPlugin(engines)
        return RuntimeAssembly(tuple(runtimes), plugin)

    def _build_link(
        self,
        link: ResolvedLinkConfig,
        created_at_ns: int,
    ) -> tuple[LinkRuntime, LinkStateEngine]:
        seed = resolve_link_seed(
            self._config.experiment.global_seed,
            link.link_id,
            link.seed_offset,
        )
        rng = random.Random(seed)
        mapper = NetworkStateMapper(self._config.network_states, rng)
        initial_state, model = self._create_model(link, rng)
        initial_parameters = mapper.sample(initial_state)
        engine = LinkStateEngine(
            model=model,
            mapper=mapper,
            rng=rng,
            refresh_parameters_when_state_unchanged=(
                self._config.controller.refresh_parameters_when_state_unchanged
            ),
        )
        runtime = LinkRuntime(
            link_id=link.link_id,
            link_type=link.link_type,
            sender_id=link.sender_id,
            edge_id=link.edge_id,
            protocol=link.protocol,
            proxy_name=link.proxy_name,
            listen=link.listen,
            advertised_host=link.advertised_host,
            advertised_port=link.advertised_port,
            upstream=link.upstream,
            current_state=initial_state,
            previous_state=initial_state,
            state_since_ns=created_at_ns,
            seed=seed,
            desired_parameters=initial_parameters,
            applied_parameters=None,
            report_enabled=link.report_enabled,
        )
        return runtime, engine

    def _create_model(
        self,
        link: ResolvedLinkConfig,
        rng: random.Random,
    ) -> tuple[NetworkState, MarkovNetworkModel | FixedNetworkModel]:
        if self._config.experiment.mode is ExperimentMode.MARKOV:
            return (
                self._config.controller.initial_state,
                MarkovNetworkModel(
                    self._config.transition.states,
                    self._config.transition.matrix,
                    rng,
                ),
            )

        fixed = self._config.fixed_state
        if fixed is None:
            raise ValueError("fixed experiment requires fixed_state configuration")
        state = fixed.overrides.get(link.link_id, fixed.default)
        return state, FixedNetworkModel(state)
