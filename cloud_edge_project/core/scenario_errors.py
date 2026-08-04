class UnsupportedScenarioError(ValueError):
    def __init__(self, scenario_type: str):
        self.scenario_type = scenario_type
        super().__init__(f"unsupported scenario_type: {scenario_type}")
