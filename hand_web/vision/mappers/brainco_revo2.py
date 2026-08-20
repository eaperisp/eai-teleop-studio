"""Compatibility import for the BrainCo Revo2 vision retargeter."""

from hand_web.vision.retargeters.brainco_revo2 import BraincoRevo2Retargeter


class BraincoRevo2VisionMapper(BraincoRevo2Retargeter):
    pass


__all__ = ["BraincoRevo2VisionMapper"]
