class ResearchEngine:
    def __init__(self):
        self.observations = []
        self.hypotheses = []

    def add_observation(self, observation):
        self.observations.append(observation)

    def add_hypothesis(self, hypothesis):
        self.hypotheses.append(hypothesis)