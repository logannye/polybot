def compute_brier_score(predicted: float, actual: int) -> float:
    return (predicted - actual) ** 2

def update_trust_weight(old_brier_ema: float, new_brier: float, alpha: float = 0.1) -> float:
    return old_brier_ema * (1 - alpha) + new_brier * alpha

def compute_calibration_correction(predictions, outcomes, bins=5):
    if not predictions:
        return {}
    bin_width = 1.0 / bins
    corrections = {}
    for b in range(bins):
        low = b * bin_width
        high = (b + 1) * bin_width
        mid = (low + high) / 2
        bin_preds, bin_outcomes = [], []
        for pred, out in zip(predictions, outcomes):
            if low <= pred < high or (b == bins - 1 and pred == high):
                bin_preds.append(pred)
                bin_outcomes.append(out)
        if len(bin_preds) < 5:
            continue
        avg_predicted = sum(bin_preds) / len(bin_preds)
        actual_rate = sum(bin_outcomes) / len(bin_outcomes)
        corrections[mid] = actual_rate - avg_predicted
    return corrections
