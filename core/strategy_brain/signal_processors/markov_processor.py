"""
Markov Persistence Filter

Tracks direction state transitions from fused signals and estimates
the probability that the current dominant state persists: p(j*, j*).

Only allows trade entry when p(j*, j*) >= min_prob (default 0.87).

This prevents entering when the signal direction is choppy/unstable,
even if individual signals look strong.
"""
from collections import deque
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from loguru import logger

from config import get_config


@dataclass
class MarkovState:
    """Result of the Markov persistence filter."""
    current_state: str            # "BULLISH" or "BEARISH"
    p_stay: float                 # p(j*, j*) — probability current state persists
    min_prob: float               # threshold from config
    passes: bool                  # True if p_stay >= min_prob
    reason: str                   # human-readable decision reason
    transitions_observed: int     # number of transitions in the window
    window_size: int              # configured window size


class MarkovPersistenceFilter:
    """
    Markov persistence filter for signal direction stability.

    Tracks recent direction states and computes a 2x2 transition matrix.
    The key metric is p(j*, j*) — the probability that the current
    dominant state persists into the next observation.

    If p(j*, j*) is too low, the signal is choppy and trading is blocked.
    """

    STATES = ("BULLISH", "BEARISH")

    def __init__(
        self,
        window: int = 50,
        min_prob: float = 0.87,
    ):
        """
        Initialize Markov filter.

        Args:
            window: Number of recent observations to track.
            min_prob: Minimum p(j*,j*) to allow trade entry.
        """
        self.window = window
        self.min_prob = min_prob

        # Sliding window of direction states
        self._state_history: deque[str] = deque(maxlen=window)

        # Transition counts: transitions[from_state][to_state] = count
        self._transitions: Dict[str, Dict[str, int]] = {
            "BULLISH": {"BULLISH": 0, "BEARISH": 0},
            "BEARISH": {"BULLISH": 0, "BEARISH": 0},
        }

        # Total transitions counted (for normalization)
        self._total_transitions = 0

        logger.info(
            f"Initialized Markov Persistence Filter: "
            f"window={window}, min_prob={min_prob}"
        )

    def observe(self, direction: str) -> None:
        """
        Record a new direction observation.

        Args:
            direction: "BULLISH" or "BEARISH"
        """
        direction = direction.upper()
        if direction not in self.STATES:
            logger.warning(f"Markov: ignoring invalid direction '{direction}'")
            return

        if self._state_history:
            prev_state = self._state_history[-1]
            self._transitions[prev_state][direction] += 1
            self._total_transitions += 1

        self._state_history.append(direction)

    def evaluate(self) -> MarkovState:
        """
        Evaluate the current Markov persistence.

        Returns:
            MarkovState with p(j*,j*), pass/fail, and reason.
        """
        min_prob = self.min_prob

        # Need at least 2 observations to have a transition
        if len(self._state_history) < 2:
            return MarkovState(
                current_state="UNKNOWN",
                p_stay=0.0,
                min_prob=min_prob,
                passes=False,
                reason="insufficient_markov_history",
                transitions_observed=0,
                window_size=self.window,
            )

        # Current dominant state = most recent observation
        current_state = self._state_history[-1]

        # Count transitions FROM current_state
        from_current = self._transitions[current_state]
        total_from_current = from_current["BULLISH"] + from_current["BEARISH"]

        if total_from_current == 0:
            # No transitions from current state yet — can't compute p(j*,j*)
            return MarkovState(
                current_state=current_state,
                p_stay=0.0,
                min_prob=min_prob,
                passes=False,
                reason="no_transitions_from_current_state",
                transitions_observed=self._total_transitions,
                window_size=self.window,
            )

        # p(j*, j*) = transitions staying in current state / total from current state
        p_stay = from_current[current_state] / total_from_current

        passes = p_stay >= min_prob

        if passes:
            reason = f"p_stay={p_stay:.4f}>=min_prob={min_prob:.4f}_PASS"
        else:
            reason = f"p_stay={p_stay:.4f}<min_prob={min_prob:.4f}_BLOCKED"

        return MarkovState(
            current_state=current_state,
            p_stay=p_stay,
            min_prob=min_prob,
            passes=passes,
            reason=reason,
            transitions_observed=self._total_transitions,
            window_size=self.window,
        )

    def get_transition_matrix(self) -> Dict[str, Dict[str, float]]:
        """
        Get the normalized transition probability matrix.

        Returns:
            Dict with transition probabilities.
        """
        result = {}
        for from_state in self.STATES:
            total = sum(self._transitions[from_state].values())
            if total > 0:
                result[from_state] = {
                    to_state: count / total
                    for to_state, count in self._transitions[from_state].items()
                }
            else:
                result[from_state] = {s: 0.0 for s in self.STATES}
        return result

    def get_stats(self) -> Dict[str, Any]:
        """Get filter statistics."""
        return {
            "window": self.window,
            "min_prob": self.min_prob,
            "observations": len(self._state_history),
            "total_transitions": self._total_transitions,
            "transition_matrix": self.get_transition_matrix(),
            "current_state": self._state_history[-1] if self._state_history else None,
        }


# Singleton
_markov_filter_instance: Optional[MarkovPersistenceFilter] = None


def get_markov_filter() -> MarkovPersistenceFilter:
    """Get or create the singleton Markov filter."""
    global _markov_filter_instance
    if _markov_filter_instance is None:
        config = get_config()
        _markov_filter_instance = MarkovPersistenceFilter(
            window=config.get("markov_window", 50),
            min_prob=config.get("min_prob", 0.87),
        )
    return _markov_filter_instance
