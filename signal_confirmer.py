"""
SIGNAL CONFIRMATION ENGINE v5.0
================================
Confirms signals from the NWS-native forecast stack already built by
weather_engine.py. This avoids duplicate network fetches and removes the
old Open-Meteo dependency from confirmation.
"""


class SignalConfirmer:
    """Confirms or rejects trading signals via NWS-native source voting."""

    def confirm_signal(
        self,
        city_info,
        target_date,
        temp_low,
        temp_high,
        our_prob=None,
        market_price_cents=0,
        ensemble_prob=None,
        distribution=None,
        todays_high=None,
    ):
        if our_prob is None:
            our_prob = ensemble_prob if ensemble_prob is not None else 0.5

        votes = {}
        details = {}
        model_means = dict((distribution or {}).get("model_means", {}) or {})

        for source_name, high in model_means.items():
            if high is None:
                votes[source_name] = "ABSTAIN"
                details[source_name] = "%s: no data -> ABSTAIN" % source_name
                continue
            vote = self._cast_vote(high, temp_low, temp_high, our_prob, gray_zone=1.5)
            votes[source_name] = vote
            details[source_name] = "%s: %.1fF [%s-%s] -> %s" % (
                source_name,
                float(high),
                temp_low,
                temp_high,
                vote,
            )

        if todays_high is not None:
            obs_vote = self._cast_observation_vote(float(todays_high), temp_low, temp_high, our_prob)
            votes["ObservedHigh"] = obs_vote
            details["ObservedHigh"] = "Observed high: %.1fF [%s-%s] -> %s" % (
                float(todays_high),
                temp_low,
                temp_high,
                obs_vote,
            )

        agree = sum(1 for vote in votes.values() if vote == "AGREE")
        disagree = sum(1 for vote in votes.values() if vote == "DISAGREE")
        anchor_agrees = votes.get("NWS Daily") == "AGREE"
        non_abstain = sum(1 for vote in votes.values() if vote != "ABSTAIN")

        if non_abstain == 0:
            verdict, mult = "REJECT", 0.0
            summary = "No forecast sources voted -> REJECT"
        elif disagree > agree:
            verdict, mult = "REJECT", 0.0
            summary = "Majority disagree (%d>%d) -> REJECT" % (disagree, agree)
        elif agree >= 3:
            verdict, mult = "STRONG", 1.5
            summary = "%d sources agree -> STRONG" % agree
        elif agree >= 2:
            verdict, mult = "CONFIRM", 1.0
            summary = "%d sources agree -> CONFIRM" % agree
        elif agree >= 1 and disagree == 0 and non_abstain >= 2:
            verdict, mult = "CONFIRM", 0.8
            summary = "%d source agrees, %d voted -> CONFIRM (cautious)" % (agree, non_abstain)
        else:
            verdict, mult = "REJECT", 0.0
            summary = "Only %d agree, %d disagree -> REJECT" % (agree, disagree)

        city_name = city_info.get("name", "?")
        print("    [CONFIRM] %s %s: %s" % (city_name, target_date, summary))

        return {
            "verdict": verdict,
            "size_multiplier": mult,
            "votes": votes,
            "details": details,
            "nws_agrees": anchor_agrees,
            "agree_count": agree,
            "disagree_count": disagree,
            "summary": summary,
        }

    def _cast_vote(self, forecast_high, temp_low, temp_high, our_prob, gray_zone=1.5):
        is_yes_signal = our_prob >= 0.5
        if temp_low <= forecast_high <= temp_high:
            return "AGREE" if is_yes_signal else "DISAGREE"

        distance = min(abs(forecast_high - temp_low), abs(forecast_high - temp_high))
        if distance <= gray_zone:
            return "ABSTAIN"

        return "DISAGREE" if is_yes_signal else "AGREE"

    def _cast_observation_vote(self, observed_high, temp_low, temp_high, our_prob):
        is_yes_signal = our_prob >= 0.5

        if observed_high > temp_high:
            return "DISAGREE" if is_yes_signal else "AGREE"
        if observed_high < temp_low:
            return "ABSTAIN"
        if temp_low <= observed_high <= temp_high:
            return "AGREE" if is_yes_signal else "ABSTAIN"
        return "ABSTAIN"
