# Minimal Virtual Power Plant Portability Validation Design

## Goal

Add the smallest executable virtual-power-plant scenario that proves the
existing platform can host two clearly different domains at the same time.
The validation must load both `bearing` and `virtual_power_plant`, run the new
scenario through existing generic scenario boundaries, and require no change
to platform core or the bearing scenario.

This is an architecture validation prototype. It is not a production virtual
power plant, a real grid controller, or a claim that all competition metrics
have been completed for the second scenario.

## Chosen Scope

Create a production-shaped, explicitly injected plugin under
`cloud_edge_project/scenarios/virtual_power_plant/`. The plugin will be
exercised by a deterministic command-line demonstration and automated tests.
It will not be added to the default production registry, so existing startup
and bearing behavior remain unchanged.

The plugin provides exactly these existing optional capabilities:

- `input_adapter`;
- `edge_inference`;
- `cloud_diagnosis`;
- `consistency_policy`;
- `arbitration_policy`;
- `storage_provider`.

No model training, external dataset, MQTT wiring, HTTP endpoint, Docker
container, frontend page, or new platform abstraction is included.

## Deterministic Scenario

The demonstration uses three regions:

| Region | Net load | Reducible power |
| --- | ---: | ---: |
| A | 320 kW | 30 kW |
| B | 410 kW | 80 kW |
| C | 270 kW | 50 kW |

The aggregate load is 1000 kW and the grid limit is 900 kW. The cloud provider
must propose a 100 kW reduction, allocating 80 kW to region B and 20 kW to
region C. The resulting load is exactly 900 kW.

The edge capability evaluates one regional observation and reports either
`within_limit` or `peak_risk`. The cloud capability evaluates the aggregate
observation and returns the reduction plan. Both use deterministic arithmetic,
because this iteration validates plugin portability rather than prediction
quality.

## Data Flow

1. A caller builds the existing scenario registry and injects the virtual
   power plant plugin through the current `plugins` parameter.
2. The registry contains both `bearing` and `virtual_power_plant`.
3. The input adapter emits scenario-neutral identity fields plus energy data
   in `evidence`.
4. The existing registry resolves the virtual-power-plant edge provider and
   cloud provider without special-case platform code.
5. Edge and cloud results are normalized into the existing generic diagnosis
   contract.
6. The unchanged consistency engine invokes the scenario policy.
7. A deliberate edge/cloud disagreement is passed to the unchanged
   arbitration engine, which invokes the scenario safety rule and returns
   `dispatch_reduction`.
8. The existing storage initialization boundary accepts the scenario storage
   provider and creates an idempotent result table in a temporary database.

## Files

New scenario files:

- `cloud_edge_project/scenarios/virtual_power_plant/__init__.py`;
- `cloud_edge_project/scenarios/virtual_power_plant/manifest.py`;
- `cloud_edge_project/scenarios/virtual_power_plant/plugin.py`;
- `cloud_edge_project/scenarios/virtual_power_plant/providers.py`.

New demonstration and tests:

- `cloud_edge_project/scripts/demo_virtual_power_plant.py`;
- `cloud_edge_project/tests/contracts/test_virtual_power_plant_plugin.py`;
- `cloud_edge_project/tests/regression/test_virtual_power_plant_flow.py`;
- `cloud_edge_project/tests/architecture/test_virtual_power_plant_portability.py`.

Existing production source files are not planned for modification. The design
and implementation plan are the only documentation additions.

## Error Handling

- Requests with another `scenario_id` fail explicitly.
- Missing, non-numeric, negative, or internally impossible energy values fail
  validation.
- An aggregate reduction request larger than the available reduction capacity
  returns an explicit degraded result rather than inventing capacity.
- Unsupported states or actions fail explicitly instead of falling back to
  bearing behavior.
- Repeated storage initialization is idempotent.

## Verification

Automated checks must prove:

- both scenario IDs are present after explicit plugin injection;
- all six declared providers resolve through the existing registry;
- the deterministic input completes edge, cloud, consistency, arbitration,
  and storage processing;
- 1000 kW is reduced by 100 kW to the 900 kW limit;
- an injected edge/cloud conflict is detected and resolved to
  `dispatch_reduction`;
- the new scenario imports no bearing or bearing-compatibility modules;
- platform core, scheduler, services, bearing code, default startup, and
  default production registry do not gain virtual-power-plant special cases;
- focused new tests and relevant existing registry/engine tests pass;
- `git diff --check` passes.

The command-line demonstration must print a compact JSON-compatible summary
containing loaded scenarios, original load, grid limit, planned reduction,
post-dispatch load, conflict status, arbitration action, and `PASS`/`FAIL`.

## Explicit Non-Goals

- real energy forecasting or AI model training;
- optimal economic dispatch;
- real hardware or grid integration;
- long-running service deployment;
- frontend visualization;
- competition-wide performance certification;
- changes to existing bearing functionality.

## Success Criteria

The work is complete when one command demonstrates `bearing` and
`virtual_power_plant` together, the new scenario traverses the existing generic
boundaries, the expected peak-reduction and conflict-resolution results are
obtained, focused tests pass, and no existing platform or bearing source file
is modified for scenario-specific behavior.
