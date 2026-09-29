"""Smooth categorical A/B/C labels without misreporting model confidence."""
import os

ALPHA = float(os.getenv("SMOOTH_ALPHA", "0.55"))
ENTER = float(os.getenv("SMOOTH_ENTER", "0.60"))
SWITCH = float(os.getenv("SMOOTH_SWITCH", "0.62"))
MARGIN = float(os.getenv("SMOOTH_MARGIN", "0.18"))
LABELS = "ABC"


class Smoother:
    def __init__(self, alpha=ALPHA, enter=ENTER, switch=SWITCH, margin=MARGIN):
        self.alpha, self.enter, self.switch, self.margin = alpha, enter, switch, margin
        self.reset()

    def reset(self, floor_tick=0):
        self.ema = None
        self.label = None
        self.last_tick = floor_tick

    def state(self):
        agreement = max(self.ema) if self.ema else None
        return {"label": self.label, "agreement": round(agreement, 4) if agreement is not None else None}

    def update(self, tick, label):
        if tick <= self.last_tick or label not in LABELS:
            return self.state()
        self.last_tick = tick
        sample = [1.0 if choice == label else 0.0 for choice in LABELS]
        a = self.alpha
        self.ema = sample if self.ema is None else [a * p + (1 - a) * e for p, e in zip(sample, self.ema)]
        candidate = LABELS[max(range(3), key=lambda i: self.ema[i])]
        if self.label is None:
            if max(self.ema) >= self.enter:
                self.label = candidate
        elif candidate != self.label:
            current = self.ema[LABELS.index(self.label)]
            strength = self.ema[LABELS.index(candidate)]
            if strength >= self.switch and strength - current >= self.margin:
                self.label = candidate
        return self.state()

    def silence(self, tick=None):
        """Clear the live verdict after its audio has left the rolling window."""
        if tick is not None:
            self.reset(max(self.last_tick, tick))
        else:
            self.ema = None
            self.label = None
        return self.state()
