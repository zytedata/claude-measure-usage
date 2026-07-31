"""Estimating output tokens that never reached the transcript.

Some turns never receive their final usage: no entry of the
``message.id`` group carries a ``stop_reason``, so even the max
``output_tokens`` across the split entries is a stale early-stream
partial — a 42KB ``Write`` can be booked as 2 output tokens (observed
only in subagent transcripts). The parser detects those groups and
hands them to :class:`OutputEstimator`, which reconstructs the real
output from content length, calibrated on the same transcript's
complete turns.

Preferred estimator: a per-model least-squares fit

    output_tokens ≈ a·text_chars + b·tool_chars + c

The per-turn constant c (~80–100 tokens in practice) absorbs fixed
overhead that otherwise depresses the apparent chars-per-token of the
many small complete turns — the reason plain ratios overshoot on the
large tool-heavy turns that dominate the missing set. On the PR #5
sample sessions the fit reconciles billed output to ±0.5% where
ratios landed at +2%.

When the transcript can't support a fit, estimation falls back to
pooled chars-per-token ratios, most-specific first: same model and
pool, same model blended, same pool across models, everything
blended, then a measured constant. A pool is the turn's dominant
content kind — tool_use input JSON tokenizes at a different density
than prose/thinking.

Estimates only ever raise a turn's output: the booked partial is a
lower bound from the API.
"""
import json

# Assistant output averages ~2.6 chars per token on real Claude Code
# sessions (measured against the CLI's billed modelUsage; JSON-heavy
# tool_use arguments tokenize denser than prose). Fallback ratio used
# only when the transcript has no usable calibration data of its own.
_OUT_CHARS_PER_TOKEN_FALLBACK = 2.6

# Minimum calibration data before a measured chars-per-token ratio is
# trusted over the next link in the fallback chain. The token floor
# alone isn't enough — a single mid-size turn can clear it, and one
# unusually dense turn would then inflate every estimate in the file
# (booked partials are lower bounds, so the failure direction is
# over-counting).
_CALIBRATION_MIN_OUT_TOKENS = 200
_CALIBRATION_MIN_TURNS = 3

_POOL_TEXT = "text"
_POOL_TOOL = "tool"

# Floors and sanity bounds for the least-squares fit. Marginal
# chars-per-token outside the bounds, or a negative/huge per-turn
# constant, means the system was ill-conditioned — fall back to
# pooled ratios instead.
_OUT_FIT_MIN_TURNS = 8
_OUT_FIT_RATIO_BOUNDS = (1.0, 10.0)
_OUT_FIT_CONST_BOUNDS = (0.0, 1000.0)


def output_content_chars(content):
    """(prose_chars, tool_chars) of model output in an assistant entry.

    Counts what output_tokens bills for, split by calibration pool:
    prose is text + thinking, tool is tool_use input JSON. The two
    tokenize at different densities, so they're kept apart for the
    ratio calibration. Non-generated fields (thinking signatures,
    redacted blocks) are excluded.
    """
    if not isinstance(content, list):
        return 0, 0
    text_chars = 0
    tool_chars = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_chars += len(block.get("text") or "")
        elif btype == "thinking":
            text_chars += len(block.get("thinking") or "")
        elif btype == "tool_use":
            try:
                tool_chars += len(
                    json.dumps(block.get("input") or {}, ensure_ascii=False)
                )
            except (TypeError, ValueError):
                pass
    return text_chars, tool_chars


def _measured_ratio(calib_entries):
    """Chars-per-token ratio over calibration entries, or None.

    Returns None when the pooled data is too thin to trust — both a
    token floor and a turn floor apply (see the constants above).
    """
    chars = sum(e[0] for e in calib_entries)
    out = sum(e[1] for e in calib_entries)
    turns = sum(e[2] for e in calib_entries)
    if out >= _CALIBRATION_MIN_OUT_TOKENS and turns >= _CALIBRATION_MIN_TURNS:
        return chars / out
    return None


def _fit_output_model(samples):
    """Least-squares (a, b, c) for out ≈ a·text + b·tool + c, or None.

    ``samples`` is a list of (text_chars, tool_chars, output_tokens)
    complete turns. Solves the 3-parameter normal equations by
    Gaussian elimination; returns None when there are too few turns,
    the system is singular (e.g. no tool content anywhere), or the
    solution fails the sanity bounds above.
    """
    if len(samples) < _OUT_FIT_MIN_TURNS:
        return None
    sxx = [[0.0] * 3 for _ in range(3)]
    sxy = [0.0] * 3
    for text, tool, out in samples:
        x = (float(text), float(tool), 1.0)
        for i in range(3):
            sxy[i] += x[i] * out
            for j in range(3):
                sxx[i][j] += x[i] * x[j]
    m = [sxx[i] + [sxy[i]] for i in range(3)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-9:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(3):
            if r != col:
                f = m[r][col] / m[col][col]
                for k in range(col, 4):
                    m[r][k] -= f * m[col][k]
    a, b, c = (m[i][3] / m[i][i] for i in range(3))
    lo, hi = _OUT_FIT_RATIO_BOUNDS
    for coef in (a, b):
        if coef <= 0 or not (lo <= 1 / coef <= hi):
            return None
    c_lo, c_hi = _OUT_FIT_CONST_BOUNDS
    if not (c_lo <= c <= c_hi):
        return None
    return a, b, c


class OutputEstimator:
    """Collects calibration data and repairs missing-usage turns.

    The parser feeds it one call per closed turn group —
    :meth:`add_complete_turn` for groups that carried a stop_reason,
    :meth:`add_missing_turn` for groups that never did — and calls
    :meth:`apply` once at finalize time.
    """

    def __init__(self):
        # (model, pool) -> [content_chars, output_tokens, turns]
        self._calibration = {}
        # model -> [(text_chars, tool_chars, output_tokens), ...]
        self._fit_samples = {}
        # (row, model, text_chars, tool_chars) awaiting estimation
        self._missing = []

    def add_complete_turn(self, model, text_chars, tool_chars, out_tokens):
        chars = text_chars + tool_chars
        if out_tokens <= 0 or chars <= 0:
            return
        pool = _POOL_TOOL if tool_chars > text_chars else _POOL_TEXT
        calib = self._calibration.setdefault((model, pool), [0, 0, 0])
        calib[0] += chars
        calib[1] += out_tokens
        calib[2] += 1
        self._fit_samples.setdefault(model, []).append(
            (text_chars, tool_chars, out_tokens)
        )

    def add_missing_turn(self, row, model, text_chars, tool_chars):
        self._missing.append((row, model, text_chars, tool_chars))

    def _pick_ratio(self, model, pool):
        """Chars-per-token ratio for a flagged turn, most-specific first.

        Same model and pool, then same model blended, then the same
        pool across models, then everything blended; each candidate
        must clear the minimum-data floors before it's trusted. The
        measured fallback constant closes the chain.
        """
        cal = self._calibration
        groups = (
            [v for (m, p), v in cal.items() if m == model and p == pool],
            [v for (m, _), v in cal.items() if m == model],
            [v for (_, p), v in cal.items() if p == pool],
            list(cal.values()),
        )
        for entries in groups:
            ratio = _measured_ratio(entries)
            if ratio:
                return ratio
        return _OUT_CHARS_PER_TOKEN_FALLBACK

    def apply(self, tokens_by_model):
        """Estimate every missing turn and book the added output.

        Flags each affected row with ``out_estimated`` (its output is
        unverified either way), raises ``row["out_tokens"]`` and the
        model bucket in ``tokens_by_model`` when the estimate beats
        the booked partial, and returns the summary dict
        ``{"turn_count": N, "added_tokens": X}``.
        """
        counts = {"turn_count": 0, "added_tokens": 0}
        fits = {}
        for row, model, text_chars, tool_chars in self._missing:
            row["out_estimated"] = True
            counts["turn_count"] += 1
            if model not in fits:
                fits[model] = _fit_output_model(
                    self._fit_samples.get(model, [])
                )
            fit = fits[model]
            if fit is not None:
                a, b, c = fit
                est = int(a * text_chars + b * tool_chars + c)
            else:
                pool = _POOL_TOOL if tool_chars > text_chars else _POOL_TEXT
                ratio = self._pick_ratio(model, pool)
                est = int((text_chars + tool_chars) / ratio) if ratio > 0 else 0
            delta = est - row["out_tokens"]
            if delta <= 0:
                continue
            bucket = tokens_by_model.get(model)
            if bucket is not None:
                bucket["output_tokens"] += delta
            row["out_tokens"] = est
            counts["added_tokens"] += delta
        return counts
