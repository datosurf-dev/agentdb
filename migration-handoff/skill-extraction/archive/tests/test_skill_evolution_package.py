def test_evolution_package_exports_all_three():
    from agentdb.skills.evolution import (
        WikiSkillAdapter,
        SkillOptAdapter,
        SkillEvolutionManager,
    )
    assert WikiSkillAdapter is not None
    assert SkillOptAdapter is not None
    assert SkillEvolutionManager is not None
