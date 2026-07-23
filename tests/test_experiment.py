from sports.research.models.experiment import Experiment


def test_experiment_creation():

    experiment = Experiment(
        name="Trade Motivation Study",
        hypothesis="Players may improve after feeling undervalued.",
        methodology="Compare performance before and after trades against historical trades.",
        metrics=[
            "efficiency_change",
            "usage_change"
        ]
    )

    assert experiment.name == "Trade Motivation Study"
    assert "historical trades" in experiment.methodology
    assert len(experiment.metrics) == 2