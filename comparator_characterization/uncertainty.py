"""Conservative repeated-estimate intervals, using the standard library only."""
from __future__ import annotations

from functools import lru_cache
import math


def _student_tail(x: float, degrees: int) -> float:
    if x <= 0:
        return .5
    upper = math.atan(math.sqrt(degrees)/x)
    constant = math.exp(math.lgamma((degrees+1)/2)-math.lgamma(degrees/2)) / math.sqrt(math.pi)
    def f(theta):
        return math.sin(theta)**(degrees-1)
    def integrate(a, b, fa, fm, fb, whole, tolerance, depth):
        middle = (a+b)/2
        left_mid, right_mid = (a+middle)/2, (middle+b)/2
        fl, fr = f(left_mid), f(right_mid)
        left = (middle-a)*(fa+4*fl+fm)/6
        right = (b-middle)*(fm+4*fr+fb)/6
        error = left+right-whole
        if depth <= 0 or abs(error) <= 15*tolerance:
            return left+right+error/15
        return (integrate(a,middle,fa,fl,fm,left,tolerance/2,depth-1)
                + integrate(middle,b,fm,fr,fb,right,tolerance/2,depth-1))
    fa, fm, fb = f(0), f(upper/2), f(upper)
    whole = upper*(fa+4*fm+fb)/6
    return constant*integrate(0,upper,fa,fm,fb,whole,
                              max(abs(whole)*1e-11, 1e-18), 24)


@lru_cache(maxsize=128)
def student_upper_quantile(tail: float, degrees: int) -> float:
    """Positive t for P(T_degrees > t)=tail; integrates the t density directly."""
    if not 0 < tail < .5 or not isinstance(degrees, int) or degrees < 1:
        raise ValueError("Student quantile needs 0<tail<0.5 and integer degrees>=1")
    low, high = 0., 1.
    while _student_tail(high, degrees) > tail:
        high *= 2
        if not math.isfinite(high):
            raise ValueError("requested confidence tail is below numeric resolution")
    for _ in range(80):
        middle = (low+high)/2
        if _student_tail(middle, degrees) > tail:
            low = middle
        else:
            high = middle
    return (low+high)/2


def classify_interval(values, *, target, tolerance, systematic_bound,
                      alpha, family_size, expected_repeats, model_validated):
    """No pass is emitted from covariance/SEM alone or with missing repeats."""
    import statistics
    result = {"repeats_valid": len(values), "mean_v": math.nan,
              "std_v": math.nan, "half_width_v": math.nan,
              "interval_low_v": math.nan, "interval_high_v": math.nan,
              "status": "not_confirmed"}
    if values:
        result["mean_v"] = float(statistics.mean(values))
    if len(values) >= 2:
        result["std_v"] = float(statistics.stdev(values))
    if tolerance is None or systematic_bound is None or target is None:
        result["status"] = "limits_not_configured"
    elif len(values) != expected_repeats or len(values) < 2:
        result["status"] = "incomplete_repeats"
    elif not model_validated:
        result["status"] = "repeat_model_not_validated"
    else:
        critical = student_upper_quantile(alpha/(2*family_size), len(values)-1)
        half = critical*result["std_v"]/math.sqrt(len(values))+systematic_bound
        low, high = result["mean_v"]-target-half, result["mean_v"]-target+half
        result.update(half_width_v=half, interval_low_v=low, interval_high_v=high)
        result["status"] = ("confirmed_within" if low >= -tolerance and high <= tolerance
                            else "confirmed_outside" if low > tolerance or high < -tolerance
                            else "not_confirmed")
    return result
