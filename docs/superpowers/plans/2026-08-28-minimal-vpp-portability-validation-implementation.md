# Minimal VPP Portability Validation Implementation Plan

1. Add a self-contained `virtual_power_plant` plugin with six existing
   capability bindings. Verify its manifest and provider set through contract
   tests.
2. Implement deterministic regional input, edge peak evaluation, aggregate
   cloud dispatch, consistency, arbitration, and storage providers. Verify the
   1000 kW to 900 kW scenario and invalid-input behavior.
3. Add a one-command demonstration that explicitly injects the plugin into the
   existing registry and prints a machine-readable result.
4. Add architecture tests proving the plugin has no bearing dependency and no
   virtual-power-plant vocabulary is added to platform core or bearing source.
5. Run focused tests, relevant existing regression tests, the demonstration,
   and `git diff --check`. Review all changes and commit only files created for
   this validation.
