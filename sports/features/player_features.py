from models.sports.player_features import PlayerFeatures


def calculate_ewma(values, alpha=0.5):

    if not values:
        return 0.0

    ewma = values[0]

    for value in values[1:]:
        ewma = alpha * value + (1 - alpha) * ewma

    return ewma


def calculate_variance(values):

    if not values:
        return 0.0

    mean = sum(values) / len(values)

    return sum(
        (x - mean) ** 2 for x in values
    ) / len(values)


class PlayerFeatureExtractor:

    def extract(self, player):

        history = player.performance_history

        if not history:
            return PlayerFeatures(
                season_average=0.0
            )

        season_average = sum(history) / len(history)

        recent = history[-5:]

        recent_average = sum(recent) / len(recent)

        return PlayerFeatures(
            season_average=season_average,
            recent_average=recent_average,
            trend=recent_average - season_average,
            variance=calculate_variance(history),
            ewma=calculate_ewma(history)
        )